"""Owner-scoped domain, durable workflow, and render worker integration."""

from __future__ import annotations

import logging
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from book_video.domain.service import DomainService
from book_video.tasks import MediaCancelledError, SQLiteIdempotencyStore, run_render_task
from book_video.workflows import (
    SQLiteCheckpointSaver,
    WorkflowCancelledError,
    create_script_generation_graph,
    run_script_generation,
)


log = logging.getLogger(__name__)

SELF_CREATED_RIGHTS = [
    {"origin": "self_created_text", "usage_status": "allowed", "license_basis": "created_in_app", "permitted_uses": ["commercial_video"], "proof_ref": "system:text-template", "expires_at": None},
    {"origin": "self_created_graphic", "usage_status": "allowed", "license_basis": "created_in_app", "permitted_uses": ["commercial_video"], "proof_ref": "system:graphic-template", "expires_at": None},
]


def _event_adapter(service: DomainService, run_id: str, *, resuming: bool):
    def emit(event: Mapping[str, Any]) -> None:
        kind = str(event["type"])
        # The API owns human review transactions. LangGraph re-emits the
        # interrupt request on resume; the persisted request is authoritative.
        if kind in {"run.created", "run.reviewed"} or (resuming and kind == "run.review_requested"):
            return
        if kind in {"run.generation_completed", "run.generation_failed", "run.cost_recorded"}:
            # A recovered graph may replay a persisted invocation before its
            # checkpoint catches up. Keep one event per actual result/cost.
            if any(
                item["type"] == kind
                and item.get("generation_result_id") == event.get("generation_result_id")
                and item.get("cost_record_id") == event.get("cost_record_id")
                for item in service.list_run_events(run_id)
            ):
                return
        current = service.get_run(run_id)
        if current["status"] in {"succeeded", "failed", "cancelled"}:
            return
        target_status, target_stage = str(event["status"]), str(event["stage"])
        if (current["status"], current["stage"]) == (target_status, target_stage):
            if kind in {
                "run.review_requested",
                "run.generation_completed",
                "run.generation_failed",
                "run.cost_recorded",
            }:
                sequence = current["sequence"] + 1
                service.persist_run_event(
                    {
                        **event,
                        "event_id": str(uuid5(NAMESPACE_URL, f"book-video:{run_id}:{kind}:{sequence}")),
                        "sequence": sequence,
                        "occurred_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    }
                )
            return
        service.transition_run(
            run_id,
            status=target_status,
            stage=target_stage,
            event_type=kind,
            script_version_id=event.get("script_version_id"),
            reason_code=event.get("reason_code"),
            message=event.get("message"),
        )

    return emit


def _script_adapter(service: DomainService, project_id: str):
    def persist(script: Mapping[str, Any]) -> None:
        service.persist_generated_script_version(project_id, script)

    return persist


def _generation_adapter(service: DomainService):
    def persist(result: Mapping[str, Any], cost: Mapping[str, Any]) -> dict[str, Any]:
        return service.persist_generation(result, cost)

    return persist


def _checkpoint_saver(service: DomainService) -> SQLiteCheckpointSaver:
    """Keep checkpoints with the domain database across worker processes."""

    return SQLiteCheckpointSaver(service.database.path)


def _render_store(service: DomainService) -> SQLiteIdempotencyStore:
    return SQLiteIdempotencyStore(service.database.path)


def _finish_run_error(service: DomainService, run_id: str, error: Exception, code: str) -> None:
    current = service.get_run(run_id)
    if current["status"] in {"succeeded", "failed", "cancelled"}:
        return
    if current.get("cancel_requested_at"):
        service.confirm_run_cancelled(run_id, str(error))
        return
    service.transition_run(
        run_id, status="failed", event_type="run.failed", reason_code=code, message=str(error)
    )


def _discard_unpublished_output(service: DomainService, result: Mapping[str, Any] | None) -> None:
    """Remove an MP4 that must not remain as a valid completed render."""

    if not result or not result.get("output_ref"):
        return
    root = service.render_output_dir.resolve()
    output = Path(str(result["output_ref"])).resolve()
    if output.is_relative_to(root):
        output.unlink(missing_ok=True)


def _discard_partial_render(service: DomainService, render_id: str) -> None:
    """Clean up a crashed render that was cancelled before task re-entry."""

    token = hashlib.sha256(render_id.encode("utf-8")).hexdigest()[:24]
    try:
        (service.render_output_dir / f"render-{token}.mp4").unlink(missing_ok=True)
    except OSError:
        log.warning("partial media for cancelled render %s could not be removed", render_id)


def _finish_render_error(
    service: DomainService, render_id: str, error: Exception, result: Mapping[str, Any] | None = None
) -> None:
    current = service.get_render(render_id)
    if current["status"] in {"succeeded", "failed", "cancelled"}:
        return
    if current.get("cancel_requested_at"):
        service.confirm_render_cancelled(render_id)
        _discard_unpublished_output(service, result)
        _discard_partial_render(service, render_id)
        return
    service.fail_render(render_id, str(error))


def process_queued_run(service: DomainService) -> bool:
    run = service.claim_next_run()
    if run is None:
        return False
    run_id, project_id = run["id"], run["project_id"]
    try:
        saver = _checkpoint_saver(service)
        if run["attempt"] > 1:
            # A retry is a new attempt of the same domain run. Keep its event
            # history, but do not resume the failed attempt's graph state.
            saver.delete_thread(run_id)
        bundle = service.get_run_snapshot(run_id)
        snapshot = dict(bundle["snapshot"])
        snapshot["run"] = run
        snapshot["asset_rights"] = SELF_CREATED_RIGHTS
        run_script_generation(
            snapshot,
            run_id=run_id,
            checkpointer=saver,
            emit_event=_event_adapter(service, run_id, resuming=False),
            persist_script=_script_adapter(service, project_id),
            persist_generation=_generation_adapter(service),
            cancel_check=lambda: service.is_run_cancel_requested(run_id),
        )
    except WorkflowCancelledError as error:
        service.confirm_run_cancelled(run_id, str(error))
    except Exception as error:
        log.exception("run %s failed", run_id)
        _finish_run_error(service, run_id, error, "generation_failed")
    return True


def process_resumable_run(service: DomainService) -> bool:
    """Reclaim an interrupted non-review graph after its worker lease expires."""

    run = service.claim_next_resumable_run()
    if run is None:
        return False
    run_id, project_id = run["id"], run["project_id"]
    try:
        if service.is_run_cancel_requested(run_id):
            service.confirm_run_cancelled(run_id, "Worker recovered a cancelled run")
            return True
        saver = _checkpoint_saver(service)
        config = {"configurable": {"thread_id": run_id}}
        if saver.get_tuple(config) is None:
            bundle = service.get_run_snapshot(run_id)
            snapshot = dict(bundle["snapshot"])
            snapshot["run"] = service.get_run(run_id)
            snapshot["asset_rights"] = SELF_CREATED_RIGHTS
            run_script_generation(
                snapshot,
                run_id=run_id,
                checkpointer=saver,
                emit_event=_event_adapter(service, run_id, resuming=False),
                persist_script=_script_adapter(service, project_id),
                persist_generation=_generation_adapter(service),
                cancel_check=lambda: service.is_run_cancel_requested(run_id),
            )
        else:
            graph = create_script_generation_graph(
                checkpointer=saver,
                emit_event=_event_adapter(service, run_id, resuming=False),
                persist_script=_script_adapter(service, project_id),
                persist_generation=_generation_adapter(service),
                cancel_check=lambda: service.is_run_cancel_requested(run_id),
            )
            graph.invoke(None, config=config)
        if service.get_run(run_id)["status"] == "running":
            raise RuntimeError("Recovered graph made no persisted state transition")
    except WorkflowCancelledError as error:
        service.confirm_run_cancelled(run_id, str(error))
    except Exception as error:
        log.exception("run %s recovery failed", run_id)
        _finish_run_error(service, run_id, error, "generation_recovery_failed")
    finally:
        service.release_run_execution_claim(run_id)
    return True


def process_reviewed_run(service: DomainService) -> bool:
    run = service.claim_next_reviewed_run()
    if run is None:
        return False
    run_id, project_id = run["id"], run["project_id"]
    try:
        if service.is_run_cancel_requested(run_id):
            service.confirm_run_cancelled(run_id, "Worker recovered a cancelled review")
            return True
        saver = _checkpoint_saver(service)
        config = {"configurable": {"thread_id": run_id}}
        if saver.get_tuple(config) is None:
            raise RuntimeError("Durable review checkpoint is unavailable")
        # The candidate may have been manually confirmed or withdrawn while
        # the graph was paused. The review gate must see the current record.
        graph = create_script_generation_graph(checkpointer=saver)
        state = graph.get_state(config)
        snapshot = dict(state.values["snapshot"])
        candidate_id = snapshot["promotion_candidate"]["id"]
        snapshot["promotion_candidate"] = service.get_promotion_candidate(candidate_id)
        graph.update_state(config, {"snapshot": snapshot})
        decision = dict(service.get_review_request(run_id))
        decision["script_version"] = service.get_script_version(decision["script_version_id"])
        result = run_script_generation(
            None,
            run_id=run_id,
            checkpointer=saver,
            review_decision=decision,
            emit_event=_event_adapter(service, run_id, resuming=True),
            persist_script=_script_adapter(service, project_id),
            persist_generation=_generation_adapter(service),
            cancel_check=lambda: service.is_run_cancel_requested(run_id),
        )
        if result.get("waiting_for_review") or result.get("review_gate_error"):
            raise RuntimeError("Review decision did not pass the current eligibility gate")
    except WorkflowCancelledError as error:
        service.confirm_run_cancelled(run_id, str(error))
    except Exception as error:
        log.exception("review resume for run %s failed", run_id)
        _finish_run_error(service, run_id, error, "review_resume_failed")
    return True


def _process_claimed_render(service: DomainService, render: Mapping[str, Any]) -> None:
    render_id = render["id"]
    result: Mapping[str, Any] | None = None
    try:
        if service.is_render_cancel_requested(render_id):
            service.confirm_render_cancelled(render_id)
            _discard_partial_render(service, render_id)
            return
        snapshot = service.get_render_snapshot(render_id)
        snapshot["width"], snapshot["height"] = 1080, 1920
        result = run_render_task(
            snapshot,
            idempotency_key=render_id,
            output_dir=service.render_output_dir,
            attempt=render["attempt"],
            idempotency_store=_render_store(service),
            audio_resolver=lambda audio_asset_id: service.get_audio_asset_media(audio_asset_id),
            cancel_check=lambda: service.is_render_cancel_requested(render_id),
        )
        if result["status"] == "succeeded":
            completed = service.complete_render(render_id, result["output_ref"])
            if completed["status"] != "succeeded":
                _discard_unpublished_output(service, result)
        else:
            service.fail_render(render_id, result.get("error_message") or "Render failed")
    except MediaCancelledError:
        service.confirm_render_cancelled(render_id)
        _discard_unpublished_output(service, result)
        _discard_partial_render(service, render_id)
    except Exception as error:
        log.exception("render %s failed", render_id)
        _finish_render_error(service, render_id, error, result)
    finally:
        service.release_render_execution_claim(render_id)


def process_queued_render(service: DomainService) -> bool:
    render = service.claim_next_render()
    if render is None:
        return False
    _process_claimed_render(service, render)
    return True


def process_resumable_render(service: DomainService) -> bool:
    """Reclaim a render left running by an exited worker."""

    render = service.claim_next_resumable_render()
    if render is None:
        return False
    _process_claimed_render(service, render)
    return True


def process_one(service: DomainService) -> bool:
    """Process one claimed task, preferring a human-reviewed run."""

    return (
        process_reviewed_run(service)
        or process_queued_run(service)
        or process_resumable_run(service)
        or process_queued_render(service)
        or process_resumable_render(service)
    )
