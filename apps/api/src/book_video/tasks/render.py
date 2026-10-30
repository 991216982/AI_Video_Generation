"""Idempotent task entry point for an approved script render."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import NAMESPACE_URL, uuid4, uuid5

from book_video.media.ffmpeg import (
    HEIGHT,
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
    MediaCancelledError,
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
        *,
        cancel_check: Callable[[], bool] | None = None,
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
        *,
        cancel_check: Callable[[], bool] | None = None,
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
                    if cancel_check is not None and cancel_check():
                        raise MediaCancelledError("render cancellation was requested")
                    self._condition.wait(timeout=0.1 if cancel_check is not None else None)
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


class IdempotentRenderError(RuntimeError):
    """A previous execution for this durable render attempt failed."""


class SQLiteIdempotencyStore:
    """Cross-process render idempotency with expiring worker leases.

    A completed attempt reuses its serialized RenderJob result after restart.
    If a worker exits without releasing its claim, another process can reclaim
    the attempt after the lease expires. Retries use a new attempt number while
    retaining the render ID and deterministic output path.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        lease_seconds: float = 300.0,
        poll_seconds: float = 0.05,
        timeout_seconds: float = 30.0,
    ) -> None:
        if str(path) == ":memory:":
            raise ValueError("SQLiteIdempotencyStore requires a file-backed database")
        if lease_seconds <= 0 or poll_seconds <= 0 or timeout_seconds <= 0:
            raise ValueError("lease_seconds, poll_seconds, and timeout_seconds must be positive")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self.timeout_seconds = timeout_seconds
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS render_idempotency ("
                "key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, "
                "lease_token TEXT, lease_expires_at REAL, result_json TEXT, error_message TEXT)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        return connection

    def _renew_lease(self, key: str, token: str, stop: threading.Event) -> None:
        interval = max(0.05, min(self.lease_seconds / 3, 30.0))
        while not stop.wait(interval):
            try:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE render_idempotency SET lease_expires_at=? "
                        "WHERE key=? AND state='running' AND lease_token=?",
                        (time.time() + self.lease_seconds, key, token),
                    )
            except sqlite3.Error:
                # Completion verifies the token so an expired lease cannot
                # silently publish a result after another worker took over.
                continue

    def execute_once(
        self,
        key: str,
        fingerprint: str,
        operation: Callable[[], dict[str, Any]],
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        token = uuid4().hex
        claimed = False
        while not claimed:
            if cancel_check is not None and cancel_check():
                raise MediaCancelledError("render cancellation was requested")
            now = time.time()
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT fingerprint, state, lease_expires_at, result_json, error_message "
                    "FROM render_idempotency WHERE key=?",
                    (key,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO render_idempotency "
                        "(key, fingerprint, state, lease_token, lease_expires_at) "
                        "VALUES (?, ?, 'running', ?, ?)",
                        (key, fingerprint, token, now + self.lease_seconds),
                    )
                    claimed = True
                else:
                    existing_fingerprint, state, lease_expires_at, result_json, error_message = row
                    if existing_fingerprint != fingerprint:
                        raise IdempotencyConflictError(
                            "idempotency key is already bound to a different render request"
                        )
                    if state == "succeeded":
                        return json.loads(result_json)
                    if state == "cancelled":
                        raise MediaCancelledError(error_message or "render cancellation was requested")
                    if state == "failed":
                        raise IdempotentRenderError(error_message or "previous render attempt failed")
                    if state != "running":
                        raise IdempotentRenderError(f"unknown durable render state: {state}")
                    if lease_expires_at is None or float(lease_expires_at) <= now:
                        updated = connection.execute(
                            "UPDATE render_idempotency SET lease_token=?, lease_expires_at=? "
                            "WHERE key=? AND fingerprint=? AND state='running' AND lease_expires_at<=?",
                            (token, now + self.lease_seconds, key, fingerprint, now),
                        )
                        claimed = updated.rowcount == 1
                connection.commit()
            if not claimed:
                time.sleep(self.poll_seconds)

        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._renew_lease,
            args=(key, token, stop_heartbeat),
            daemon=True,
        )
        heartbeat.start()
        try:
            result = operation()
            result_json = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except BaseException as error:
            state = "cancelled" if isinstance(error, MediaCancelledError) else "failed"
            with self._connect() as connection:
                connection.execute(
                    "UPDATE render_idempotency SET state=?, lease_token=NULL, lease_expires_at=NULL, "
                    "error_message=? WHERE key=? AND state='running' AND lease_token=?",
                    (state, str(error), key, token),
                )
            raise
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=1.0)

        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE render_idempotency SET state='succeeded', lease_token=NULL, "
                "lease_expires_at=NULL, result_json=?, error_message=NULL "
                "WHERE key=? AND state='running' AND lease_token=?",
                (result_json, key, token),
            )
            if updated.rowcount != 1:
                row = connection.execute(
                    "SELECT state, result_json FROM render_idempotency WHERE key=? AND fingerprint=?",
                    (key, fingerprint),
                ).fetchone()
                if row and row[0] == "succeeded":
                    return json.loads(row[1])
                raise IdempotentRenderError("render idempotency lease was lost before completion")
        return result


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
    refs = script.get("fact_revision_refs")
    if refs is not None:
        current = {
            str(fact.get("key")): fact
            for fact in book.get("facts", []) if isinstance(fact, Mapping)
        }
        for ref in refs:
            if not isinstance(ref, Mapping) or str(ref.get("key")) not in current:
                raise RenderSnapshotError("script fact revision is unavailable")
            fact = current[str(ref["key"])]
            if fact.get("verification_status") not in {"source_verified", "human_confirmed"}:
                raise RenderSnapshotError("script fact revision has been withdrawn")
            if not str(fact.get("source_note") or "").strip() and not fact.get("evidence_id"):
                raise RenderSnapshotError("script fact revision has no basis")
            if int(ref.get("revision") or 0) != int(fact.get("revision") or 1):
                raise RenderSnapshotError("script fact revision is stale")

    if "audio_media_path" in snapshot or "audio_path" in snapshot:
        raise RenderSnapshotError("client media paths are not accepted")
    audio_id = snapshot.get("audio_asset_id")
    audio = snapshot.get("audio_asset")
    if audio_id is None:
        if audio is not None:
            raise RenderSnapshotError("audio metadata requires audio_asset_id")
    else:
        if not isinstance(audio_id, str) or not audio_id or not isinstance(audio, Mapping):
            raise RenderSnapshotError("audio asset must be resolved by the server")
        if str(audio.get("id")) != audio_id or str(audio.get("owner_user_id")) != str(project.get("owner_user_id")):
            raise RenderSnapshotError("audio asset owner or ID mismatch")
        if audio.get("origin") not in {"self_recorded_voice", "licensed_voice"}:
            raise RenderSnapshotError("audio asset origin is not permitted")
        if audio.get("usage_status") != "allowed" or audio.get("rights_attested") is not True:
            raise RenderSnapshotError("audio asset rights are blocked")
        if not str(audio.get("rights_proof_note") or "").strip() or audio.get("permitted_uses") != "render_and_manual_publish":
            raise RenderSnapshotError("audio asset permission proof is incomplete")
        expires_at = audio.get("expires_at")
        if audio.get("origin") == "licensed_voice" and not expires_at:
            raise RenderSnapshotError("licensed audio requires an expiry")
        if expires_at:
            try:
                expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if expires.tzinfo is None or expires.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                    raise ValueError("expired")
            except ValueError as error:
                raise RenderSnapshotError("audio asset permission is expired or invalid") from error

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
    attempt: int | None = None,
    idempotency_store: IdempotencyStore | None = None,
    renderer: Callable[..., dict[str, Any]] = render_mp4,
    audio_resolver: Callable[[str], Path] | None = None,
    cancel_check: Callable[[], bool] | None = None,
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
    render_snapshot = normalized.get("render", {})
    if attempt is None:
        attempt = render_snapshot.get("attempt", 1) if isinstance(render_snapshot, Mapping) else 1
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise RenderSnapshotError("attempt must be a positive integer")
    immutable_input = {
        key: normalized.get(key)
        for key in (
            "project",
            "book",
            "promotion_candidate",
            "script_version",
            "asset_rights",
            "width",
            "height",
            "audio_asset_id",
            "audio_asset",
        )
    }
    request_fingerprint = _fingerprint(immutable_input)
    render_token = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    render_id = str(uuid5(NAMESPACE_URL, f"book-video:render:{idempotency_key}"))
    output_path = Path(output_dir) / f"render-{render_token}.mp4"
    attempt_key = f"{idempotency_key}:attempt:{attempt}"

    def check_cancelled() -> None:
        if cancel_check is not None and cancel_check():
            raise MediaCancelledError("render cancellation was requested")

    def operation() -> dict[str, Any]:
        check_cancelled()
        audio_path = None
        if normalized.get("audio_asset_id") is not None:
            if audio_resolver is None:
                raise RenderSnapshotError("approved audio requires a server media resolver")
            audio_path = audio_resolver(str(normalized["audio_asset_id"]))
            if not isinstance(audio_path, Path) or not audio_path.is_file():
                raise RenderSnapshotError("approved audio file is unavailable")
        # A retry retains the render ID and output path. Remove any incomplete
        # file left by a failed prior attempt before starting the next one.
        output_path.unlink(missing_ok=True)
        base = {
            "id": render_id,
            "project_id": str(normalized["project"]["id"]),
            "script_version_id": str(normalized["script_version"]["id"]),
            "audio_asset_id": normalized.get("audio_asset_id"),
            "owner_user_id": str(normalized["project"]["owner_user_id"]),
            "created_at": _timestamp(),
        }
        try:
            renderer_options = {
                "audio_path": audio_path,
                "audio_origin": normalized["audio_asset"]["origin"],
            } if audio_path is not None else {}
            metadata = renderer(
                normalized["script_version"]["segments"],
                duration_seconds=duration,
                output_path=output_path,
                width=normalized.get("width", WIDTH),
                height=normalized.get("height", HEIGHT),
                cancel_check=cancel_check,
                **renderer_options,
            )
        except MediaCancelledError:
            output_path.unlink(missing_ok=True)
            raise
        except (MediaDependencyError, MediaRenderError) as error:
            output_path.unlink(missing_ok=True)
            return {
                **base,
                "status": "failed",
                "output_ref": None,
                "error_message": str(error),
            }
        try:
            check_cancelled()
        except MediaCancelledError:
            output_path.unlink(missing_ok=True)
            raise
        return {
            **base,
            "status": "succeeded",
            "output_ref": str(output_path),
            "error_message": None,
            "media": metadata,
        }

    check_cancelled()
    return store.execute_once(
        attempt_key,
        request_fingerprint,
        operation,
        cancel_check=cancel_check,
    )
