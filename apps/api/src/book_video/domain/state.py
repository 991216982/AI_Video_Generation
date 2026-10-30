"""Finite-state rules shared by API and worker-facing domain services."""

from __future__ import annotations

from enum import Enum

from .errors import DomainError, conflict


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStage(str, Enum):
    PREPARE = "prepare"
    SCRIPT = "script"
    FACT_CHECK = "fact_check"
    REVIEW = "review"
    STORYBOARD = "storyboard"
    RENDER = "render"
    COMPLETE = "complete"


class RunEventType(str, Enum):
    CREATED = "run.created"
    STAGE_CHANGED = "run.stage_changed"
    REVIEW_REQUESTED = "run.review_requested"
    REVIEWED = "run.reviewed"
    FAILED = "run.failed"
    COMPLETED = "run.completed"
    CANCEL_REQUESTED = "run.cancel_requested"
    CANCELLED = "run.cancelled"
    RETRY_REQUESTED = "run.retry_requested"
    GENERATION_COMPLETED = "run.generation_completed"
    GENERATION_FAILED = "run.generation_failed"
    COST_RECORDED = "run.cost_recorded"


ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_REVIEW,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.WAITING_REVIEW: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED}
    ),
    RunStatus.FAILED: frozenset({RunStatus.QUEUED, RunStatus.CANCELLED}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def validate_run_transition(current: str, target: str) -> None:
    try:
        current_status = RunStatus(current)
        target_status = RunStatus(target)
    except ValueError as exc:
        raise DomainError(422, "invalid_run_status", "Unknown run status") from exc
    if current_status == target_status:
        return
    if target_status not in ALLOWED_RUN_TRANSITIONS[current_status]:
        raise conflict(
            "invalid_state_transition",
            f"Run status cannot move from {current_status.value} to {target_status.value}",
            current_status=current_status.value,
            target_status=target_status.value,
        )


RENDER_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset(
        {RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.RUNNING: frozenset(
        {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.WAITING_REVIEW: frozenset(),
    RunStatus.FAILED: frozenset({RunStatus.QUEUED}),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


def validate_render_transition(current: str, target: str) -> None:
    try:
        current_status = RunStatus(current)
        target_status = RunStatus(target)
    except ValueError as exc:
        raise DomainError(422, "invalid_render_status", "Unknown render status") from exc
    if current_status == target_status:
        return
    if target_status not in RENDER_TRANSITIONS[current_status]:
        raise conflict(
            "invalid_state_transition",
            f"Render status cannot move from {current_status.value} to {target_status.value}",
            current_status=current_status.value,
            target_status=target_status.value,
        )
