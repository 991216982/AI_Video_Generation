"""Stage 1 adapter between the owner-scoped domain and the generation worker.

Run events and generated scripts are persisted by DomainService. The LangGraph
checkpointer is process-local for the first single-user pilot; the worker must
stay alive while a run waits for human review.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from book_video.domain.service import DomainService
from book_video.tasks import run_render_task
from book_video.workflows import run_script_generation


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
        current = service.get_run(run_id)
        if current["status"] in {"succeeded", "failed", "cancelled"}:
            return
        target_status, target_stage = str(event["status"]), str(event["stage"])
        if (current["status"], current["stage"]) == (target_status, target_stage):
            if kind == "run.review_requested":
                sequence = current["sequence"] + 1
                service.persist_run_event(
                    {
                        **event,
                        "event_id": str(uuid5(NAMESPACE_URL, f"book-video:{run_id}:review-request:{sequence}")),
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


def process_queued_run(service: DomainService) -> bool:
    run = service.claim_next_run()
    if run is None:
        return False
    run_id, project_id = run["id"], run["project_id"]
    try:
        bundle = service.get_run_snapshot(run_id)
        snapshot = dict(bundle["snapshot"])
        snapshot["run"] = run
        snapshot["asset_rights"] = SELF_CREATED_RIGHTS
        run_script_generation(
            snapshot,
            run_id=run_id,
            emit_event=_event_adapter(service, run_id, resuming=False),
            persist_script=_script_adapter(service, project_id),
        )
    except Exception as error:
        log.exception("run %s failed", run_id)
        service.transition_run(run_id, status="failed", event_type="run.failed", reason_code="generation_failed", message=str(error))
    return True


def process_reviewed_run(service: DomainService) -> bool:
    run = service.claim_next_reviewed_run()
    if run is None:
        return False
    run_id, project_id = run["id"], run["project_id"]
    try:
        decision = dict(service.get_review_request(run_id))
        decision["script_version"] = service.get_script_version(decision["script_version_id"])
        run_script_generation(
            None,
            run_id=run_id,
            review_decision=decision,
            emit_event=_event_adapter(service, run_id, resuming=True),
            persist_script=_script_adapter(service, project_id),
        )
    except Exception as error:
        log.exception("review resume for run %s failed", run_id)
        service.transition_run(run_id, status="failed", event_type="run.failed", reason_code="review_resume_failed", message=str(error))
    return True


def process_queued_render(service: DomainService) -> bool:
    render = service.claim_next_render()
    if render is None:
        return False
    render_id = render["id"]
    try:
        snapshot = service.get_render_snapshot(render_id)
        snapshot["width"], snapshot["height"] = 1080, 1920
        result = run_render_task(
            snapshot,
            idempotency_key=render_id,
            output_dir=service.render_output_dir,
        )
        if result["status"] == "succeeded":
            service.complete_render(render_id, result["output_ref"])
        else:
            service.fail_render(render_id, result.get("error_message") or "Render failed")
    except Exception as error:
        log.exception("render %s failed", render_id)
        service.fail_render(render_id, str(error))
    return True


def process_one(service: DomainService) -> bool:
    """Process one claimed task, preferring a human-reviewed run."""

    return (
        process_reviewed_run(service)
        or process_queued_run(service)
        or process_queued_render(service)
    )
