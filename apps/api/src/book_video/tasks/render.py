"""Idempotent task entry point for an approved script render."""

from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import NAMESPACE_URL, uuid5

from book_video.media.ffmpeg import (
    HEIGHT,
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
    WIDTH,
    MediaDependencyError,
    MediaRenderError,
    render_mp4,
)
from book_video.media.rights import AssetRightsError, validate_asset_rights


class IdempotencyConflictError(ValueError):
    """An idempotency key was reused with a different render request."""


class RenderSnapshotError(ValueError):
    """The resolved render snapshot is incomplete or not approved."""


class IdempotencyStore(Protocol):
    def execute_once(
        self,
        key: str,
        fingerprint: str,
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]: ...


class InMemoryIdempotencyStore:
    """Thread-safe process-local idempotency store for development/tests.

    Production or multi-worker integration should provide a durable store
    implementing ``execute_once`` with an atomic claim and persisted result.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._records: dict[str, dict[str, Any]] = {}

    def execute_once(
        self,
        key: str,
        fingerprint: str,
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        with self._condition:
            while True:
                record = self._records.get(key)
                if record is None:
                    self._records[key] = {"fingerprint": fingerprint, "state": "running"}
                    break
                if record["fingerprint"] != fingerprint:
                    raise IdempotencyConflictError("idempotency key is already bound to a different request")
                if record["state"] == "running":
                    self._condition.wait()
                    continue
                if record["state"] == "succeeded":
                    return deepcopy(record["result"])
                if record["state"] == "failed":
                    raise record["error"]

        try:
            result = operation()
        except BaseException as error:
            with self._condition:
                self._records[key] = {"fingerprint": fingerprint, "state": "failed", "error": error}
                self._condition.notify_all()
            raise
        with self._condition:
            self._records[key] = {
                "fingerprint": fingerprint,
                "state": "succeeded",
                "result": deepcopy(result),
            }
            self._condition.notify_all()
        return result


_DEFAULT_IDEMPOTENCY_STORE = InMemoryIdempotencyStore()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fingerprint(payload: Mapping[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validate_snapshot(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    project = snapshot.get("project")
    book = snapshot.get("book")
    candidate = snapshot.get("promotion_candidate")
    script = snapshot.get("script_version")
    if not all(isinstance(item, Mapping) for item in (project, book, candidate, script)):
        raise RenderSnapshotError(
            "snapshot must include project, book, promotion_candidate, and script_version mappings"
        )
    if script.get("review_status") != "approved":
        raise RenderSnapshotError("script must be approved before rendering")
    if str(script.get("project_id", "")) != str(project.get("id", "")):
        raise RenderSnapshotError("script_version.project_id must match project.id")
    if str(project.get("book_id", "")) != str(book.get("id", "")):
        raise RenderSnapshotError("project and book edition do not match")
    if str(candidate.get("book_id", "")) != str(book.get("id", "")):
        raise RenderSnapshotError("promotion candidate does not match the book edition")
    if str(project.get("promotion_candidate_id", "")) != str(candidate.get("id", "")):
        raise RenderSnapshotError("project must reference the supplied promotion candidate")
    if candidate.get("eligibility_status") != "manually_confirmed":
        raise RenderSnapshotError("promotion candidate eligibility must be manually confirmed before rendering")
    if str(project.get("target_platform", "")) != str(candidate.get("platform", "")):
        raise RenderSnapshotError("project and promotion candidate platforms do not match")

    width = snapshot.get("width", WIDTH)
    height = snapshot.get("height", HEIGHT)
    if width != WIDTH or height != HEIGHT:
        raise RenderSnapshotError(f"render dimensions must be exactly {WIDTH}x{HEIGHT}")
    duration = project.get("desired_seconds")
    if isinstance(duration, bool) or not isinstance(duration, int) or not MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
        raise RenderSnapshotError("project.desired_seconds must be an integer from 20 to 45")

    segments = script.get("segments")
    if not isinstance(segments, list) or not segments:
        raise RenderSnapshotError("approved script must include at least one segment")
    durations = [float(segment.get("duration_seconds", 0)) for segment in segments if isinstance(segment, Mapping)]
    if len(durations) != len(segments) or any(value <= 0 for value in durations):
        raise RenderSnapshotError("script segment durations must be positive")
    if abs(sum(durations) - duration) > 0.05:
        raise RenderSnapshotError("script segment durations must sum to project.desired_seconds")

    verified_keys = {
        str(fact.get("key"))
        for fact in book.get("facts", [])
        if isinstance(fact, Mapping)
        and fact.get("verification_status") in {"source_verified", "human_confirmed"}
    }
    used_keys = {str(key) for segment in segments for key in segment.get("fact_keys", [])}
    if not used_keys or not used_keys.issubset(verified_keys):
        raise RenderSnapshotError("script references a fact that is not verified")

    rights = snapshot.get("asset_rights", [])
    if not isinstance(rights, list):
        raise RenderSnapshotError("asset_rights must be a list")
    try:
        validate_asset_rights(
            rights,
            required_origins=("self_created_text", "self_created_graphic"),
        )
    except AssetRightsError as error:
        raise RenderSnapshotError(str(error)) from error
    return deepcopy(dict(snapshot)), duration


def run_render_task(
    snapshot: Mapping[str, Any],
    *,
    idempotency_key: str,
    output_dir: str | Path,
    idempotency_store: IdempotencyStore | None = None,
    renderer: Callable[..., dict[str, Any]] = render_mp4,
) -> dict[str, Any]:
    """Render an approved snapshot once and return RenderJob-shaped metadata.

    This accepts already-resolved plain mappings, keeping the generation
    package independent from API, database, and domain-service modules.
    ``idempotency_store`` can be backed by the integration layer's database;
    the default is a process-local development store.
    """

    if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
        raise RenderSnapshotError("idempotency_key must contain 8 to 128 characters")
    normalized, duration = _validate_snapshot(snapshot)
    store = idempotency_store or _DEFAULT_IDEMPOTENCY_STORE
    request_fingerprint = _fingerprint(normalized)
    render_token = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    render_id = str(uuid5(NAMESPACE_URL, f"book-video:render:{idempotency_key}"))
    output_path = Path(output_dir) / f"render-{render_token}.mp4"

    def operation() -> dict[str, Any]:
        base = {
            "id": render_id,
            "project_id": str(normalized["project"]["id"]),
            "script_version_id": str(normalized["script_version"]["id"]),
            "owner_user_id": str(normalized["project"]["owner_user_id"]),
            "created_at": _timestamp(),
        }
        try:
            metadata = renderer(
                normalized["script_version"]["segments"],
                duration_seconds=duration,
                output_path=output_path,
                width=normalized.get("width", WIDTH),
                height=normalized.get("height", HEIGHT),
            )
        except (MediaDependencyError, MediaRenderError) as error:
            return {
                **base,
                "status": "failed",
                "output_ref": None,
                "error_message": str(error),
            }
        return {
            **base,
            "status": "succeeded",
            "output_ref": str(output_path),
            "error_message": None,
            "media": metadata,
        }

    return store.execute_once(idempotency_key, request_fingerprint, operation)
