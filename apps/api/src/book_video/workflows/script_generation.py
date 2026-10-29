"""Deterministic two-variant script generation with a resumable review gate.

The workflow only uses verified bibliographic facts and the creator's own
opinion. It deliberately has no model, network, TTS, or media-provider call.
Integration code supplies persistence and event callbacks, plus a durable
LangGraph checkpointer when resume must survive a worker restart.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence, TypedDict
from uuid import NAMESPACE_URL, uuid5

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from book_video.media.rights import asset_rights_blockers


EventSink = Callable[[Mapping[str, Any]], None]
ScriptSink = Callable[[Mapping[str, Any]], None]
CancelCheck = Callable[[], bool]

SAFE_FACT_KEYS = {
    "title",
    "author",
    "publisher",
    "edition_label",
    "isbn",
    "publication_year",
    "page_count",
}


class ScriptGenerationError(ValueError):
    """The supplied project snapshot is incomplete or unsafe to generate."""


class ReviewGateError(ScriptGenerationError):
    """A requested approval did not satisfy the required human checks."""


class WorkflowCancelledError(RuntimeError):
    """The owning run has a persisted cancellation request."""


class GenerationState(TypedDict, total=False):
    run_id: str
    snapshot: dict[str, Any]
    project_id: str
    scripts: list[dict[str, Any]]
    events: list[dict[str, Any]]
    event_sequence: int
    status: str
    stage: str
    review_request: dict[str, Any]
    review_gate_error: list[str]
    approved_script_version_id: str
    review_outcome: str


_DEFAULT_CHECKPOINTER = MemorySaver()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _check_cancelled(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None and cancel_check():
        raise WorkflowCancelledError("run cancellation was requested")


def _verified_metadata_fact(book: Mapping[str, Any]) -> dict[str, str]:
    facts = book.get("facts")
    if not isinstance(facts, list):
        raise ScriptGenerationError("book.facts must be a list")

    for raw_fact in facts:
        if not isinstance(raw_fact, Mapping):
            continue
        key = str(raw_fact.get("key", "")).strip()
        status = raw_fact.get("verification_status")
        value = str(raw_fact.get("value", "")).strip()
        if key not in SAFE_FACT_KEYS or status not in {"source_verified", "human_confirmed"} or not value:
            continue
        # Prefer matching the edition metadata so an unrelated fact cannot be
        # presented as the book's title, author, or publisher.
        metadata_value = book.get(key)
        if metadata_value is not None and str(metadata_value).strip() != value:
            continue
        return {"key": key, "value": value}

    raise ScriptGenerationError(
        "at least one verified bibliographic fact is required; unverified or book-text facts are not used"
    )


def _validate_snapshot(snapshot: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    copied = deepcopy(dict(snapshot))
    project = copied.get("project")
    book = copied.get("book")
    candidate = copied.get("promotion_candidate")
    if not all(isinstance(value, Mapping) for value in (project, book, candidate)):
        raise ScriptGenerationError("snapshot must include project, book, and promotion_candidate mappings")

    project_id = str(project.get("id", ""))
    book_id = str(book.get("id", ""))
    candidate_id = str(candidate.get("id", ""))
    owner_user_id = str(project.get("owner_user_id", ""))
    if not all((project_id, book_id, candidate_id, owner_user_id)):
        raise ScriptGenerationError("project, book, candidate, and owner IDs are required")
    if str(project.get("book_id", "")) != book_id or str(candidate.get("book_id", "")) != book_id:
        raise ScriptGenerationError("project and promotion candidate must reference the same book edition")
    if str(project.get("promotion_candidate_id", "")) != candidate_id:
        raise ScriptGenerationError("project must reference the supplied promotion candidate")
    if project.get("target_platform") != candidate.get("platform"):
        raise ScriptGenerationError("project and promotion candidate platforms must match")

    desired_seconds = project.get("desired_seconds")
    if isinstance(desired_seconds, bool) or not isinstance(desired_seconds, int) or not 20 <= desired_seconds <= 45:
        raise ScriptGenerationError("project.desired_seconds must be an integer from 20 to 45")
    if not str(book.get("title", "")).strip():
        raise ScriptGenerationError("book.title is required")
    original_opinion = str(book.get("original_opinion", "")).strip()
    if not original_opinion:
        raise ScriptGenerationError("book.original_opinion is required")

    fact = _verified_metadata_fact(book)
    run_snapshot = copied.get("run", {})
    if isinstance(run_snapshot, Mapping) and run_snapshot.get("id") and str(run_snapshot["id"]) != run_id:
        raise ScriptGenerationError("run_id does not match snapshot.run.id")
    existing_scripts = copied.get("script_versions", [])
    if not isinstance(existing_scripts, list):
        raise ScriptGenerationError("snapshot.script_versions must be a list when provided")
    copied.setdefault("run", {})
    return copied


def _make_event(
    *,
    run_id: str,
    project_id: str,
    sequence: int,
    event_type: str,
    status: str,
    stage: str,
    script_version_id: str | None = None,
    reason_code: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid5(NAMESPACE_URL, f"book-video:{run_id}:event:{sequence}")),
        "run_id": run_id,
        "project_id": project_id,
        "sequence": sequence,
        "type": event_type,
        "status": status,
        "stage": stage,
        "occurred_at": _utc_now(),
        "script_version_id": script_version_id,
        "render_id": None,
        "reason_code": reason_code,
        "message": message,
    }


def _verified_fact_keys(snapshot: Mapping[str, Any]) -> set[str]:
    book = snapshot["book"]
    result: set[str] = set()
    for fact in book.get("facts", []):
        if not isinstance(fact, Mapping):
            continue
        key = str(fact.get("key", "")).strip()
        value = str(fact.get("value", "")).strip()
        if key not in SAFE_FACT_KEYS or not value:
            continue
        if fact.get("verification_status") not in {"source_verified", "human_confirmed"}:
            continue
        metadata_value = book.get(key)
        if metadata_value is not None and str(metadata_value).strip() != value:
            continue
        result.add(key)
    return result


def _build_segments(
    *,
    variant: str,
    fact: Mapping[str, str],
    opinion: str,
    duration: int,
) -> list[dict[str, Any]]:
    if variant == "A":
        content = [
            ("你会怎样画出今天的路线？", "你会怎样画出今天的路线？", []),
            (f"核对信息：{fact['key']}｜{fact['value']}", f"先看一条已核对的信息：{fact['key']}，{fact['value']}。", [fact["key"]]),
            (opinion, f"这是我的原创看法：{opinion}", []),
            ("你会怎么做？欢迎聊聊自己的方法。", "你会怎么做？欢迎在评论区聊聊自己的方法。", []),
        ]
    else:
        content = [
            ("今天不下结论，先聊一个观察。", "今天不下结论，先聊一个观察。", []),
            (opinion, f"这是我对这本书的原创看法：{opinion}", []),
            (f"已核对的书籍信息：{fact['key']}｜{fact['value']}", f"再核对一条书籍信息：{fact['key']}，{fact['value']}。", [fact["key"]]),
            ("把它当作一个问题，试着找到你的答案。", "把它当作一个问题，试着找到你的答案。", []),
        ]

    weights = [0.16, 0.22, 0.38, 0.24]
    durations = [round(duration * weight, 3) for weight in weights[:-1]]
    durations.append(round(duration - sum(durations), 3))
    return [
        {
            "order": index,
            "onscreen_text": onscreen,
            "narration": narration,
            "fact_keys": fact_keys,
            "duration_seconds": durations[index],
        }
        for index, (onscreen, narration, fact_keys) in enumerate(content)
    ]


def _generated_script_ids(run_id: str) -> dict[str, str]:
    return {
        variant: str(uuid5(NAMESPACE_URL, f"book-video:{run_id}:script:{variant}"))
        for variant in ("A", "B")
    }


def _script_version_numbers(snapshot: Mapping[str, Any], run_id: str) -> dict[str, int]:
    """Allocate project versions and preserve assignments on run replays."""

    generated_ids = _generated_script_ids(run_id)
    generated_by_id = {script_id: variant for variant, script_id in generated_ids.items()}
    known_project_versions: dict[str, int] = {}
    for item in snapshot.get("script_versions", []):
        if not isinstance(item, Mapping):
            continue
        version_number = item.get("version_number")
        if isinstance(version_number, bool) or not isinstance(version_number, int) or version_number < 1:
            continue
        script_id = str(item.get("id", ""))
        if script_id:
            known_project_versions[script_id] = version_number

    current_run_versions = {
        generated_by_id[script_id]: version_number
        for script_id, version_number in known_project_versions.items()
        if script_id in generated_by_id
    }
    prior_max = max(
        (
            number
            for script_id, number in known_project_versions.items()
            if script_id not in generated_by_id
        ),
        default=0,
    )

    if "A" in current_run_versions:
        first_version = current_run_versions["A"]
    elif "B" in current_run_versions:
        first_version = current_run_versions["B"] - 1
    else:
        first_version = prior_max + 1
    if first_version < 1:
        raise ScriptGenerationError("snapshot.script_versions has an invalid version for this run")

    allocated = {
        variant: current_run_versions.get(variant, first_version + index)
        for index, variant in enumerate(("A", "B"))
    }
    if allocated["A"] == allocated["B"]:
        raise ScriptGenerationError("snapshot contains conflicting version numbers for this run")
    occupied_by_other = {
        number
        for script_id, number in known_project_versions.items()
        if script_id not in generated_by_id
    }
    if any(number in occupied_by_other for number in allocated.values()):
        raise ScriptGenerationError("snapshot contains a conflicting script version number")
    return allocated


def _make_scripts(snapshot: Mapping[str, Any], run_id: str) -> list[dict[str, Any]]:
    project = snapshot["project"]
    book = snapshot["book"]
    fact = _verified_metadata_fact(book)
    opinion = str(book["original_opinion"]).strip()
    prohibited = [str(item).strip().casefold() for item in project.get("prohibited_claims", []) if str(item).strip()]
    candidates = [
        {
            "variant": "A",
            "title_suggestion": "给日常选择画一张路线图",
            "description_suggestion": "从一条已核对的书籍信息和创作者原创观点出发，邀请观众分享自己的方法。",
        },
        {
            "variant": "B",
            "title_suggestion": "从一个观察开始聊起",
            "description_suggestion": "一段基于创作者原创观点的荐书分享；发布前请人工核对书籍版本与拟挂商品。",
        },
    ]
    script_ids = _generated_script_ids(run_id)
    version_numbers = _script_version_numbers(snapshot, run_id)
    result: list[dict[str, Any]] = []
    for item in candidates:
        segments = _build_segments(
            variant=item["variant"],
            fact=fact,
            opinion=opinion,
            duration=project["desired_seconds"],
        )
        script_text = "\n".join(
            [item["title_suggestion"], item["description_suggestion"]]
            + [str(segment["onscreen_text"]) + "\n" + str(segment["narration"]) for segment in segments]
        ).casefold()
        matches = [claim for claim in prohibited if claim in script_text]
        if matches:
            raise ScriptGenerationError(
                "generated script contains a prohibited claim: " + ", ".join(matches)
            )

        script_id = script_ids[item["variant"]]
        result.append(
            {
                "id": script_id,
                "project_id": str(project["id"]),
                "owner_user_id": str(project["owner_user_id"]),
                "version_number": version_numbers[item["variant"]],
                "based_on_version_id": None,
                "segments": segments,
                "title_suggestion": item["title_suggestion"],
                "description_suggestion": item["description_suggestion"],
                "review_status": "pending",
                "created_at": _utc_now(),
            }
        )
    return result


def _script_content_blockers(
    snapshot: Mapping[str, Any], script: Mapping[str, Any]
) -> list[str]:
    blockers: list[str] = []
    project = snapshot["project"]
    if str(script.get("project_id", "")) != str(project.get("id", "")):
        blockers.append("script_project_mismatch")
    if str(script.get("owner_user_id", "")) != str(project.get("owner_user_id", "")):
        blockers.append("script_owner_mismatch")
    if not str(script.get("title_suggestion", "")).strip() or not str(
        script.get("description_suggestion", "")
    ).strip():
        blockers.append("script_metadata_missing")

    segments = script.get("segments")
    if not isinstance(segments, list) or not segments:
        return blockers + ["script_segments_invalid"]
    durations: list[float] = []
    used_fact_keys: set[str] = set()
    text_parts = [str(script.get("title_suggestion", "")), str(script.get("description_suggestion", ""))]
    for segment in segments:
        if not isinstance(segment, Mapping):
            blockers.append("script_segment_invalid")
            continue
        if not isinstance(segment.get("onscreen_text"), str) or not isinstance(segment.get("narration"), str):
            blockers.append("script_segment_text_invalid")
        text_parts.extend([str(segment.get("onscreen_text", "")), str(segment.get("narration", ""))])
        raw_keys = segment.get("fact_keys", [])
        if not isinstance(raw_keys, list):
            blockers.append("script_fact_keys_invalid")
        else:
            used_fact_keys.update(str(key) for key in raw_keys)
        try:
            duration = float(segment.get("duration_seconds", 0))
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            blockers.append("script_segment_duration_invalid")
        durations.append(duration)

    desired_seconds = project.get("desired_seconds")
    if (
        isinstance(desired_seconds, bool)
        or not isinstance(desired_seconds, int)
        or not MIN_SCRIPT_SECONDS <= desired_seconds <= MAX_SCRIPT_SECONDS
        or abs(sum(durations) - desired_seconds) > 0.05
    ):
        blockers.append("script_duration_invalid")
    if not used_fact_keys or not used_fact_keys.issubset(_verified_fact_keys(snapshot)):
        blockers.append("script_facts_not_verified")

    prohibited = [
        str(item).strip().casefold()
        for item in project.get("prohibited_claims", [])
        if str(item).strip()
    ]
    all_text = "\n".join(text_parts).casefold()
    if any(claim in all_text for claim in prohibited):
        blockers.append("script_contains_prohibited_claim")
    return blockers


MIN_SCRIPT_SECONDS = 20
MAX_SCRIPT_SECONDS = 45


def _approval_blockers(
    snapshot: Mapping[str, Any],
    scripts: Sequence[Mapping[str, Any]],
    script_version_id: str,
    selected_script: Mapping[str, Any] | None = None,
) -> list[str]:
    blockers: list[str] = []
    candidate = snapshot["promotion_candidate"]
    if candidate.get("eligibility_status") != "manually_confirmed":
        blockers.append("promotion_candidate_unverified")
    selected = selected_script or next(
        (script for script in scripts if str(script.get("id")) == script_version_id), None
    )
    if selected is None:
        blockers.append("script_version_not_in_run")
    else:
        if str(selected.get("id", "")) != script_version_id:
            blockers.append("script_version_id_mismatch")
        original_ids = {str(item.get("id", "")) for item in scripts}
        if script_version_id not in original_ids and str(selected.get("based_on_version_id", "")) not in original_ids:
            blockers.append("script_version_not_derived_from_run")
        blockers.extend(_script_content_blockers(snapshot, selected))

    rights = snapshot.get("asset_rights", [])
    if not isinstance(rights, list):
        blockers.append("asset_rights_invalid")
    else:
        blockers.extend(
            asset_rights_blockers(
                rights,
                required_origins=("self_created_text", "self_created_graphic"),
            )
        )
    return blockers


def _normalise_review(decision: Any) -> dict[str, Any]:
    if not isinstance(decision, Mapping):
        raise ReviewGateError("review decision must be a mapping")
    normalized = {
        "script_version_id": str(decision.get("script_version_id", "")),
        "decision": str(decision.get("decision", "")),
        "note": decision.get("note"),
        "fact_confirmed": decision.get("fact_confirmed") is True,
        "product_match_confirmed": decision.get("product_match_confirmed") is True,
        "asset_rights_confirmed": decision.get("asset_rights_confirmed") is True,
    }
    if decision.get("script_version") is not None:
        if not isinstance(decision["script_version"], Mapping):
            raise ReviewGateError("script_version must be a mapping when provided")
        normalized["script_version"] = deepcopy(dict(decision["script_version"]))
    if normalized["decision"] not in {"approve", "reject"}:
        raise ReviewGateError("decision must be approve or reject")
    if not normalized["script_version_id"]:
        raise ReviewGateError("script_version_id is required")
    return normalized


def _resolve_review_script(
    snapshot: Mapping[str, Any],
    scripts: Sequence[Mapping[str, Any]],
    decision: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve an original or API-saved edit that directly derives from it."""

    requested_id = str(decision["script_version_id"])
    originals = {str(item.get("id", "")): item for item in scripts}
    original = originals.get(requested_id)
    supplied = decision.get("script_version")
    if supplied is None:
        if original is None:
            raise ReviewGateError("script_version_id must identify a version from this run")
        return deepcopy(dict(original))

    if not isinstance(supplied, Mapping) or str(supplied.get("id", "")) != requested_id:
        raise ReviewGateError("script_version_id must match the supplied script_version.id")
    project = snapshot["project"]
    if str(supplied.get("project_id", "")) != str(project.get("id", "")):
        raise ReviewGateError("supplied script version belongs to a different project")
    if str(supplied.get("owner_user_id", "")) != str(project.get("owner_user_id", "")):
        raise ReviewGateError("supplied script version belongs to a different owner")

    if original is not None:
        for field in ("segments", "title_suggestion", "description_suggestion", "based_on_version_id"):
            if supplied.get(field) != original.get(field):
                raise ReviewGateError("original run scripts cannot be replaced by edited content")
        return deepcopy(dict(original))

    based_on_id = str(supplied.get("based_on_version_id", ""))
    if based_on_id not in originals:
        raise ReviewGateError("edited script must be based on an original version from this run")
    version_number = supplied.get("version_number")
    if isinstance(version_number, bool) or not isinstance(version_number, int) or version_number < 1:
        raise ReviewGateError("edited script version_number must be a positive integer")
    review_status = supplied.get("review_status")
    if review_status not in {"pending", "approved", "rejected"}:
        raise ReviewGateError("edited script has an invalid review_status")
    if decision["decision"] == "approve" and review_status == "rejected":
        raise ReviewGateError("a rejected edited script cannot be approved")
    if decision["decision"] == "reject" and review_status == "approved":
        raise ReviewGateError("an approved edited script cannot be rejected")
    return deepcopy(dict(supplied))


def create_script_generation_graph(
    *,
    checkpointer: Any = None,
    emit_event: EventSink | None = None,
    persist_script: ScriptSink | None = None,
    cancel_check: CancelCheck | None = None,
) -> Any:
    """Compile the generation graph with caller-provided integration hooks.

    Both callbacks should be idempotent. Event IDs are stable for a given run
    and sequence; script IDs are stable for a run and variant. A durable
    checkpointer can be supplied by the integration layer; the default saver
    is intentionally process-local for development and tests.
    """

    event_sink = emit_event or (lambda _event: None)
    script_sink = persist_script or (lambda _script: None)

    def append_event(
        state: GenerationState,
        *,
        sequence: int,
        event_type: str,
        status: str,
        stage: str,
        script_version_id: str | None = None,
        reason_code: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        event = _make_event(
            run_id=state["run_id"],
            project_id=str(
                state.get("project_id")
                or state.get("snapshot", {}).get("project", {}).get("id", "")
            ),
            sequence=sequence,
            event_type=event_type,
            status=status,
            stage=stage,
            script_version_id=script_version_id,
            reason_code=reason_code,
            message=message,
        )
        event_sink(event)
        return event

    def prepare(state: GenerationState) -> dict[str, Any]:
        _check_cancelled(cancel_check)
        snapshot = _validate_snapshot(state["snapshot"], state["run_id"])
        project = snapshot["project"]
        run_snapshot = snapshot.get("run", {})
        initial_sequence = int(run_snapshot.get("sequence", 0)) if isinstance(run_snapshot, Mapping) else 0
        event = append_event(
            state,
            sequence=initial_sequence + 1,
            event_type="run.stage_changed",
            status="running",
            stage="script",
            message="开始生成两版原创脚本",
        )
        return {
            "snapshot": snapshot,
            "project_id": str(project["id"]),
            "event_sequence": event["sequence"],
            "events": [event],
            "status": "running",
            "stage": "script",
        }

    def generate(state: GenerationState) -> dict[str, Any]:
        _check_cancelled(cancel_check)
        scripts = _make_scripts(state["snapshot"], state["run_id"])
        # Check once after pure generation and before persistence begins. The
        # domain sink still owns the final atomic cancellation guard.
        _check_cancelled(cancel_check)
        for script in scripts:
            script_sink(script)
        sequence = state["event_sequence"] + 1
        event = append_event(
            state,
            sequence=sequence,
            event_type="run.stage_changed",
            status="running",
            stage="fact_check",
            message="两版原创脚本已保存，正在校验事实引用",
        )
        return {
            "scripts": scripts,
            "event_sequence": event["sequence"],
            "events": state.get("events", []) + [event],
            "stage": "fact_check",
            "status": "running",
        }

    def fact_check(state: GenerationState) -> dict[str, Any]:
        _check_cancelled(cancel_check)
        verified = _verified_fact_keys(state["snapshot"])
        for script in state["scripts"]:
            used = {str(key) for segment in script["segments"] for key in segment["fact_keys"]}
            if not used or not used.issubset(verified):
                raise ScriptGenerationError("fact check rejected a script with an unverified fact reference")

        sequence = state["event_sequence"] + 1
        event = append_event(
            state,
            sequence=sequence,
            event_type="run.stage_changed",
            status="running",
            stage="review",
            message="脚本事实引用已校验，等待创作者人工审核",
        )
        blocker_hint = _approval_blockers(
            state["snapshot"], state["scripts"], str(state["scripts"][0]["id"])
        )
        return {
            "event_sequence": event["sequence"],
            "events": state.get("events", []) + [event],
            "stage": "review",
            "status": "waiting_review",
            "review_request": {
                "run_id": state["run_id"],
                "script_versions": [
                    {"id": item["id"], "version_number": item["version_number"], "review_status": item["review_status"]}
                    for item in state["scripts"]
                ],
                "approval_blockers": blocker_hint,
            },
        }

    def review(state: GenerationState) -> dict[str, Any]:
        _check_cancelled(cancel_check)
        requested_sequence = state["event_sequence"] + 1
        requested_event = append_event(
            state,
            sequence=requested_sequence,
            event_type="run.review_requested",
            status="waiting_review",
            stage="review",
            message="请核对事实、拟挂商品与素材许可后选择一个脚本版本",
        )
        decision = _normalise_review(
            interrupt(
                {
                    "run_id": state["run_id"],
                    "project_id": state["project_id"],
                    "script_versions": state["review_request"]["script_versions"],
                    "approval_blockers": state["review_request"]["approval_blockers"],
                    "required_confirmations": [
                        "fact_confirmed",
                        "product_match_confirmed",
                        "asset_rights_confirmed",
                    ],
                }
            )
        )
        _check_cancelled(cancel_check)
        selected_id = decision["script_version_id"]
        selected = _resolve_review_script(state["snapshot"], state["scripts"], decision)
        if decision["decision"] == "approve":
            blockers = _approval_blockers(
                state["snapshot"], state["scripts"], selected_id, selected_script=selected
            )
            if selected.get("review_status") == "rejected":
                blockers.append("script_already_rejected")
            if not all(
                decision[field]
                for field in ("fact_confirmed", "product_match_confirmed", "asset_rights_confirmed")
            ):
                blockers.append("review_confirmations_missing")
        else:
            blockers = []

        if decision["decision"] == "approve" and blockers:
            reviewed_event = append_event(
                state,
                sequence=requested_sequence + 1,
                event_type="run.reviewed",
                status="waiting_review",
                stage="review",
                script_version_id=selected_id,
                reason_code="review_gate_blocked",
                message="审核通过条件未满足，仍需处理阻塞项",
            )
            request = dict(state["review_request"])
            request["approval_blockers"] = list(dict.fromkeys(blockers))
            return {
                "event_sequence": reviewed_event["sequence"],
                "events": state.get("events", []) + [requested_event, reviewed_event],
                "review_request": request,
                "review_gate_error": request["approval_blockers"],
                "status": "waiting_review",
                "stage": "review",
            }

        if decision["decision"] == "reject":
            selected["review_status"] = "rejected"
            script_sink(selected)
            reviewed_scripts = [
                selected if str(item.get("id")) == selected_id else item
                for item in state["scripts"]
            ]
            if selected_id not in {str(item.get("id")) for item in state["scripts"]}:
                reviewed_scripts.append(selected)
            reviewed_event = append_event(
                state,
                sequence=requested_sequence + 1,
                event_type="run.reviewed",
                status="waiting_review",
                stage="review",
                script_version_id=selected_id,
                message="创作者拒绝了所选脚本版本",
            )
            cancelled_event = append_event(
                state,
                sequence=requested_sequence + 2,
                event_type="run.cancelled",
                status="cancelled",
                stage="review",
                script_version_id=selected_id,
                reason_code="script_rejected",
                message="本次脚本生成运行已因人工拒绝而结束",
            )
            return {
                "scripts": reviewed_scripts,
                "event_sequence": cancelled_event["sequence"],
                "events": state.get("events", []) + [requested_event, reviewed_event, cancelled_event],
                "status": "cancelled",
                "stage": "review",
                "review_outcome": "rejected",
            }

        selected["review_status"] = "approved"
        script_sink(selected)
        reviewed_scripts = [
            selected if str(item.get("id")) == selected_id else item
            for item in state["scripts"]
        ]
        if selected_id not in {str(item.get("id")) for item in state["scripts"]}:
            reviewed_scripts.append(selected)
        reviewed_event = append_event(
            state,
            sequence=requested_sequence + 1,
            event_type="run.reviewed",
            status="running",
            stage="review",
            script_version_id=selected_id,
            message="人工审核已通过，已锁定审核通过的脚本版本",
        )
        completed_event = append_event(
            state,
            sequence=requested_sequence + 2,
            event_type="run.completed",
            status="succeeded",
            stage="complete",
            script_version_id=selected_id,
            message="脚本生成与人工审核流程已完成",
        )
        return {
            "scripts": reviewed_scripts,
            "event_sequence": completed_event["sequence"],
            "events": state.get("events", []) + [requested_event, reviewed_event, completed_event],
            "status": "succeeded",
            "stage": "complete",
            "approved_script_version_id": selected_id,
            "review_outcome": "approved",
            "review_gate_error": [],
        }

    def after_review(state: GenerationState) -> str:
        return "review" if state.get("review_gate_error") else END

    builder = StateGraph(GenerationState)
    builder.add_node("prepare", prepare)
    builder.add_node("generate", generate)
    builder.add_node("fact_check", fact_check)
    builder.add_node("review", review)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "generate")
    builder.add_edge("generate", "fact_check")
    builder.add_edge("fact_check", "review")
    builder.add_conditional_edges("review", after_review, {"review": "review", END: END})
    return builder.compile(
        checkpointer=checkpointer if checkpointer is not None else _DEFAULT_CHECKPOINTER
    )


def _public_checkpoint_state(graph: Any, run_id: str) -> dict[str, Any] | None:
    """Read an existing run without re-running nodes or repeating side effects."""

    state = graph.get_state({"configurable": {"thread_id": run_id}})
    if not state.values:
        return None
    public = {key: value for key, value in state.values.items() if not key.startswith("__")}
    if str(public.get("run_id", "")) != run_id:
        raise ScriptGenerationError("checkpoint thread does not match the requested run")

    interrupts = [
        interrupt
        for task in (state.tasks or ())
        for interrupt in (getattr(task, "interrupts", ()) or ())
    ]
    if interrupts:
        public["waiting_for_review"] = True
        public["review_interrupt"] = interrupts[0].value
        # The interrupting node has not returned its output yet, so expose its
        # stable request event in the public state without emitting it again.
        events = list(public.get("events", []))
        current_sequence = int(public.get("event_sequence", 0))
        request_sequence = current_sequence + 1
        if not any(int(event.get("sequence", -1)) == request_sequence for event in events):
            events.append(
                _make_event(
                    run_id=run_id,
                    project_id=str(public.get("project_id", "")),
                    sequence=request_sequence,
                    event_type="run.review_requested",
                    status="waiting_review",
                    stage="review",
                    message="请核对事实、拟挂商品与素材许可后选择一个脚本版本",
                )
            )
            public["events"] = events
    else:
        public["waiting_for_review"] = public.get("status") == "waiting_review"
    return public


def run_script_generation(
    snapshot: Mapping[str, Any] | None,
    *,
    run_id: str,
    checkpointer: Any = None,
    emit_event: EventSink | None = None,
    persist_script: ScriptSink | None = None,
    review_decision: Mapping[str, Any] | None = None,
    cancel_check: CancelCheck | None = None,
) -> dict[str, Any]:
    """Start a run or resume it with the decision returned by a human reviewer.

    Call again with the same ``run_id`` and checkpointer to resume. On start,
    provide the pure-data snapshot; on resume, ``review_decision`` is wrapped
    in LangGraph's ``Command(resume=...)``. The returned ``waiting_review``
    state contains two editable pending ScriptVersion-shaped dictionaries.
    """

    if not run_id.strip():
        raise ScriptGenerationError("run_id is required")
    graph = create_script_generation_graph(
        checkpointer=checkpointer,
        emit_event=emit_event,
        persist_script=persist_script,
        cancel_check=cancel_check,
    )

    _check_cancelled(cancel_check)
    if review_decision is None:
        recovered = _public_checkpoint_state(graph, run_id)
        if recovered is not None:
            if snapshot is not None:
                stored_project = recovered.get("snapshot", {}).get("project", {})
                supplied_project = snapshot.get("project", {})
                for field in ("id", "owner_user_id"):
                    if str(stored_project.get(field, "")) != str(supplied_project.get(field, "")):
                        raise ScriptGenerationError(
                            "checkpoint project or owner does not match the supplied run snapshot"
                        )
            return recovered
        if snapshot is None:
            raise ScriptGenerationError("snapshot is required to start a run")
        run_snapshot = snapshot.get("run", {})
        initial_sequence = int(run_snapshot.get("sequence", 0)) if isinstance(run_snapshot, Mapping) else 0
        initial_state: Any = {
            "run_id": run_id,
            "snapshot": deepcopy(dict(snapshot)),
            "event_sequence": initial_sequence,
            "events": [],
            "scripts": [],
            "status": "queued",
            "stage": "prepare",
        }
    else:
        initial_state = Command(resume=dict(review_decision))

    result = graph.invoke(initial_state, config={"configurable": {"thread_id": run_id}})
    public = {key: value for key, value in result.items() if not key.startswith("__")}
    interrupts = result.get("__interrupt__", ())
    if interrupts:
        public["waiting_for_review"] = True
        public["review_interrupt"] = interrupts[0].value
        # The request event is delivered through emit_event before interrupt.
        # Include it in the returned view even though the node itself pauses.
        if public.get("events"):
            current_sequence = int(public["events"][-1]["sequence"])
            if current_sequence + 1 not in {event["sequence"] for event in public["events"]}:
                public["events"] = list(public["events"]) + [
                    _make_event(
                        run_id=run_id,
                        project_id=public["project_id"],
                        sequence=current_sequence + 1,
                        event_type="run.review_requested",
                        status="waiting_review",
                        stage="review",
                        message="请核对事实、拟挂商品与素材许可后选择一个脚本版本",
                    )
                ]
        return public

    public["waiting_for_review"] = public.get("status") == "waiting_review"
    return public
