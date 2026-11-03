"""Owner-scoped domain operations used by HTTP handlers and worker adapters."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import base64
import binascii
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from book_video.api.schemas import ScriptVersionCreate
from book_video.connectors.manual import import_csv
from book_video.domain.errors import DomainError, conflict, not_found
from book_video.workflows.style_extraction import extract_style_features, style_guidance
from book_video.domain.state import (
    RunEventType,
    RunStage,
    RunStatus,
    validate_render_transition,
    validate_run_transition,
)
from book_video.persistence.database import Database


JsonObject = dict[str, Any]
SubmissionHook = Callable[[JsonObject], None]
MANUALLY_CONFIRMED = "manually_confirmed"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _dump(value: Any) -> JsonObject:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _payload(row: Any) -> JsonObject:
    return json.loads(row["payload"])


def _request_hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _as_uuid(value: UUID | str) -> str:
    try:
        return str(value if isinstance(value, UUID) else UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise DomainError(422, "invalid_id", "A valid UUID is required") from exc


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DomainError(422, "invalid_timestamp", "Timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc)


class DomainService:
    """Persistence-backed domain entry point for API, orchestration, and render workers.

    Public methods never accept owner IDs from records. The owner is supplied by the
    server-side identity adapter when this service is constructed.
    """

    def __init__(
        self,
        database: Database | None = None,
        *,
        owner_user_id: UUID | str | None = None,
        render_output_dir: str | Path | None = None,
    ) -> None:
        from book_video.connectors.manual.identity import development_owner_user_id

        self.database = database or Database()
        self.owner_user_id = _as_uuid(owner_user_id or development_owner_user_id())
        configured_output_dir = (
            render_output_dir
            or os.getenv("BOOK_VIDEO_OUTPUT_DIR")
            or self._default_render_output_dir()
        )
        self.render_output_dir = Path(configured_output_dir).expanduser().resolve()

    @staticmethod
    def _default_render_output_dir() -> Path:
        return Path.cwd() / ".local" / "renders"

    def create_book(self, data: Any) -> JsonObject:
        book_data = _dump(data)
        with self.database.transaction() as conn:
            for fact in book_data["facts"]:
                evidence_id = fact.get("evidence_id")
                if evidence_id and self._get(conn, "evidence", evidence_id) is None:
                    raise not_found("Evidence")
            book_id = str(uuid4())
            created_at = _utc_now()
            result = {
                **book_data,
                "id": book_id,
                "owner_user_id": self.owner_user_id,
                "created_at": created_at,
            }
            result["facts"] = [{**fact, "revision": 1} for fact in book_data["facts"]]
            conn.execute(
                "INSERT INTO books(id, owner_user_id, created_at, payload) VALUES (?, ?, ?, ?)",
                (book_id, self.owner_user_id, created_at, _json(result)),
            )
            for fact in result["facts"]:
                revision = {**fact, "book_id": book_id, "created_at": created_at,
                            "created_by_user_id": self.owner_user_id}
                conn.execute(
                    "INSERT INTO book_fact_revisions(book_id, owner_user_id, fact_key, revision, payload) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (book_id, self.owner_user_id, fact["key"], _json(revision)),
                )
        return result

    def revise_book_fact(self, book_id: UUID | str, fact_key: str, data: Any) -> JsonObject:
        book_key = _as_uuid(book_id)
        request = _dump(data)
        if not fact_key or "/" in fact_key:
            raise DomainError(422, "validation_error", "Invalid fact key")
        status = request["verification_status"]
        note = (request.get("source_note") or "").strip()
        evidence_id = request.get("evidence_id")
        if status == "human_confirmed" and not note:
            raise DomainError(422, "validation_error", "Human confirmation requires a note")
        if status == "source_verified" and not (note or evidence_id):
            raise DomainError(422, "validation_error", "Source verification requires a note or evidence")
        with self.database.transaction() as conn:
            row = self._get_row(conn, "books", book_key)
            if row is None:
                raise not_found("Book edition")
            book = _payload(row)
            facts = book.get("facts", [])
            current = next((fact for fact in facts if fact["key"] == fact_key), None)
            if current is None:
                raise not_found("Book fact")
            if evidence_id and self._get(conn, "evidence", evidence_id) is None:
                raise not_found("Evidence")
            current_revision = int(current.get("revision", 1))
            if request["expected_revision"] != current_revision:
                raise conflict("revision_conflict", "Book fact changed since it was loaded",
                               expected_revision=request["expected_revision"], current_revision=current_revision)
            next_revision = current_revision + 1
            now = _utc_now()
            revision = {"book_id": book_key, "key": fact_key, "revision": next_revision,
                        "value": request["value"], "verification_status": status,
                        "source_note": request.get("source_note"), "evidence_id": evidence_id,
                        "created_at": now, "created_by_user_id": self.owner_user_id}
            book["facts"] = [
                {key: revision[key] for key in ("key", "value", "verification_status", "source_note", "evidence_id", "revision")}
                if fact["key"] == fact_key else fact for fact in facts
            ]
            conn.execute("UPDATE books SET payload = ? WHERE id = ? AND owner_user_id = ?",
                         (_json(book), book_key, self.owner_user_id))
            conn.execute("INSERT INTO book_fact_revisions(book_id, owner_user_id, fact_key, revision, payload) "
                         "VALUES (?, ?, ?, ?, ?)",
                         (book_key, self.owner_user_id, fact_key, next_revision, _json(revision)))
        return revision

    def list_book_fact_revisions(self, book_id: UUID | str, fact_key: str) -> list[JsonObject]:
        book_key = _as_uuid(book_id)
        with self.database.read() as conn:
            book = self._get(conn, "books", book_key)
            if book is None or not any(fact["key"] == fact_key for fact in book.get("facts", [])):
                raise not_found("Book fact")
            rows = conn.execute(
                "SELECT payload FROM book_fact_revisions WHERE book_id = ? AND owner_user_id = ? "
                "AND fact_key = ? ORDER BY revision",
                (book_key, self.owner_user_id, fact_key),
            ).fetchall()
        return [_payload(row) for row in rows]

    def get_book(self, book_id: UUID | str) -> JsonObject:
        return self._get_or_404("books", book_id, "Book edition")

    def list_books(
        self, *, limit: int = 20, cursor: str | None = None, q: str | None = None
    ) -> JsonObject:
        self._validate_page_limit(limit)
        normalized_query = (q or "").strip().casefold() or None
        if normalized_query and len(normalized_query) > 100:
            raise DomainError(422, "invalid_query", "Book search query is too long")
        filters = {"resource": "books", "q": normalized_query}
        position = self._decode_page_cursor(cursor, filters) if cursor else None
        sql = (
            "SELECT id, created_at, payload FROM books WHERE owner_user_id = ?"
        )
        args: list[Any] = [self.owner_user_id]
        if position:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            args.extend([position["created_at"], position["created_at"], position["id"]])
        sql += " ORDER BY created_at DESC, id DESC"
        with self.database.read() as conn:
            rows = conn.execute(sql, args).fetchall()
        matching: list[tuple[Any, JsonObject]] = []
        for row in rows:
            book = _payload(row)
            if normalized_query is not None:
                searchable = " ".join(
                    str(book.get(field) or "") for field in ("title", "author", "isbn")
                ).casefold()
                if normalized_query not in searchable:
                    continue
            matching.append((row, book))
        page = matching[:limit]
        next_cursor = (
            self._encode_page_cursor(filters, page[-1][0])
            if len(matching) > limit and page
            else None
        )
        return {"items": [book for _row, book in page], "next_cursor": next_cursor}

    def list_creative_projects(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        book_id: UUID | str | None = None,
    ) -> JsonObject:
        self._validate_page_limit(limit)
        book_key = _as_uuid(book_id) if book_id is not None else None
        filters = {"resource": "creative_projects", "book_id": book_key}
        position = self._decode_page_cursor(cursor, filters) if cursor else None
        sql = (
            "SELECT id, created_at, payload FROM creative_projects WHERE owner_user_id = ?"
        )
        args: list[Any] = [self.owner_user_id]
        if book_key is not None:
            sql += " AND book_id = ?"
            args.append(book_key)
        if position:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            args.extend([position["created_at"], position["created_at"], position["id"]])
        sql += " ORDER BY created_at DESC, id DESC"
        with self.database.read() as conn:
            rows = conn.execute(sql, args).fetchall()
        page_rows = rows[: limit + 1]
        has_more = len(page_rows) > limit
        page_rows = page_rows[:limit]
        return {
            "items": [_payload(row) for row in page_rows],
            "next_cursor": (
                self._encode_page_cursor(filters, page_rows[-1])
                if has_more and page_rows
                else None
            ),
        }

    def _encode_page_cursor(self, filters: JsonObject, row: Any) -> str:
        payload = {
            "version": 1,
            "owner_user_id": self.owner_user_id,
            "filters_hash": _request_hash(filters),
            "created_at": row["created_at"],
            "id": row["id"],
        }
        encoded = base64.urlsafe_b64encode(_json(payload).encode("utf-8"))
        return encoded.decode("ascii").rstrip("=")

    def _decode_page_cursor(
        self, cursor: str | None, filters: JsonObject
    ) -> JsonObject:
        try:
            if not cursor or len(cursor) > 2048:
                raise ValueError("invalid cursor length")
            raw = base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
            )
            payload = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("version") != 1
                or payload.get("owner_user_id") != self.owner_user_id
                or payload.get("filters_hash") != _request_hash(filters)
            ):
                raise ValueError("cursor does not match this owner or query")
            created_at = payload["created_at"]
            position_id = _as_uuid(payload["id"])
            _timestamp(created_at)
            if not isinstance(created_at, str):
                raise ValueError("invalid timestamp")
            return {"created_at": created_at, "id": position_id}
        except DomainError as exc:
            raise DomainError(422, "invalid_cursor", "Cursor is invalid for this query") from exc
        except (ValueError, TypeError, KeyError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise DomainError(422, "invalid_cursor", "Cursor is invalid for this query") from exc

    @staticmethod
    def _validate_page_limit(limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise DomainError(422, "invalid_limit", "Page limit must be between 1 and 100")

    def create_promotion_candidate(self, data: Any) -> JsonObject:
        candidate_data = _dump(data)
        book_id = _as_uuid(candidate_data["book_id"])
        if candidate_data.get("eligibility_status", "unverified") != "unverified" or candidate_data.get("eligibility_checked_at") is not None or "confirmation_basis" in candidate_data:
            raise DomainError(422, "validation_error", "Promotion candidate must be created unverified")
        with self.database.transaction() as conn:
            book = self._get(conn, "books", book_id)
            if book is None:
                raise not_found("Book edition")
            candidate_id = str(uuid4())
            created_at = _utc_now()
            result = {
                **candidate_data,
                "book_id": book_id,
                "id": candidate_id,
                "owner_user_id": self.owner_user_id,
                "created_at": created_at,
                "eligibility_status": "unverified",
                "eligibility_checked_at": None,
                "eligibility_note": candidate_data.get("eligibility_note"),
                "eligibility_revision": 1,
                "eligibility_checked_by_user_id": None,
            }
            conn.execute(
                "INSERT INTO promotion_candidates"
                "(id, owner_user_id, book_id, created_at, eligibility_revision, "
                "eligibility_checked_by_user_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    self.owner_user_id,
                    book_id,
                    created_at,
                    result["eligibility_revision"],
                    result["eligibility_checked_by_user_id"],
                    _json(result),
                ),
            )
        return result

    def get_promotion_candidate(self, candidate_id: UUID | str) -> JsonObject:
        return self._get_or_404("promotion_candidates", candidate_id, "Promotion candidate")

    def upload_audio_asset(
        self, content: bytes, *, sha256: str, origin: str, rights_attested: bool,
        rights_proof_note: str, permitted_uses: str, expires_at: str | None = None,
        declared_mime_type: str | None = None,
    ) -> JsonObject:
        if not isinstance(content, bytes) or not content or len(content) > 10 * 1024 * 1024:
            raise DomainError(422, "validation_error", "Audio must be 1 byte to 10 MiB")
        if not re.fullmatch(r"[a-f0-9]{64}", sha256) or hashlib.sha256(content).hexdigest() != sha256:
            raise DomainError(422, "validation_error", "Audio SHA-256 does not match")
        if origin not in {"self_recorded_voice", "licensed_voice"} or not rights_attested or not rights_proof_note.strip() or permitted_uses != "render_and_manual_publish":
            raise DomainError(422, "asset_rights_blocked", "Audio rights declaration is incomplete")
        expiry = _timestamp(expires_at) if expires_at else None
        if origin == "licensed_voice" and expiry is None:
            raise DomainError(422, "asset_rights_blocked", "Licensed voice requires an expiry")
        if expiry is not None and expiry <= datetime.now(timezone.utc):
            raise DomainError(422, "asset_rights_blocked", "Audio permission has expired")
        private_dir = self.render_output_dir.parent / "audio" / self.owner_user_id
        private_dir.mkdir(parents=True, exist_ok=True)
        # Probe and finalize on the destination volume. Windows cannot rename a
        # tempfile from the system drive into a workspace on another drive.
        with tempfile.NamedTemporaryFile(suffix=".audio", dir=private_dir, delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        try:
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_format", "-show_streams",
                     "-of", "json", str(temporary_path)],
                    capture_output=True, text=True, timeout=15, check=True,
                )
                media = json.loads(probe.stdout)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise DomainError(422, "validation_error", "Audio format could not be verified") from exc
            streams = media.get("streams", [])
            if not any(stream.get("codec_type") == "audio" for stream in streams) or any(stream.get("codec_type") == "video" for stream in streams):
                raise DomainError(422, "validation_error", "Upload must contain audio only")
            format_name = media.get("format", {}).get("format_name", "").split(",")
            if "wav" in format_name:
                mime_type, extension = "audio/wav", ".wav"
            elif "mp3" in format_name:
                mime_type, extension = "audio/mpeg", ".mp3"
            elif "mov" in format_name or "mp4" in format_name:
                mime_type, extension = "audio/mp4", ".m4a"
            else:
                raise DomainError(422, "validation_error", "Unsupported audio format")
            if declared_mime_type and declared_mime_type != mime_type:
                raise DomainError(422, "validation_error", "Declared audio MIME does not match media format")
            try:
                duration_ms = round(float(media["format"]["duration"]) * 1000)
            except (KeyError, TypeError, ValueError) as exc:
                raise DomainError(422, "validation_error", "Audio duration is unavailable") from exc
            if not 1 <= duration_ms <= 45000:
                raise DomainError(422, "validation_error", "Audio duration must not exceed 45 seconds")
            asset_id = str(uuid4())
            now = _utc_now()
            storage_path = private_dir / f"{asset_id}{extension}"
            temporary_path.replace(storage_path)
            result = {
                "id": asset_id, "owner_user_id": self.owner_user_id, "origin": origin,
                "sha256": sha256, "mime_type": mime_type, "duration_ms": duration_ms,
                "rights_attested": True, "rights_proof_note": rights_proof_note.strip(),
                "permitted_uses": permitted_uses,
                "expires_at": expiry.isoformat().replace("+00:00", "Z") if expiry else None,
                "usage_status": "allowed", "created_at": now,
            }
            try:
                with self.database.transaction() as conn:
                    conn.execute(
                        "INSERT INTO audio_assets(id, owner_user_id, sha256, storage_path, created_at, payload) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (asset_id, self.owner_user_id, sha256, str(storage_path), now, _json(result)),
                    )
            except BaseException:
                storage_path.unlink(missing_ok=True)
                raise
            return result
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _current_audio_status(asset: JsonObject) -> JsonObject:
        allowed = bool(asset.get("rights_attested") and asset.get("rights_proof_note")
                       and asset.get("permitted_uses") == "render_and_manual_publish")
        expiry = asset.get("expires_at")
        if asset.get("origin") == "licensed_voice" and not expiry:
            allowed = False
        if expiry and _timestamp(expiry) <= datetime.now(timezone.utc):
            allowed = False
        return {**asset, "usage_status": "allowed" if allowed else "blocked"}

    def get_audio_asset(self, audio_asset_id: UUID | str) -> JsonObject:
        asset = self._get_or_404("audio_assets", audio_asset_id, "Audio asset")
        return self._current_audio_status(asset)

    def list_audio_assets(self) -> list[JsonObject]:
        with self.database.read() as conn:
            rows = conn.execute("SELECT payload FROM audio_assets WHERE owner_user_id = ? ORDER BY created_at DESC, id DESC", (self.owner_user_id,)).fetchall()
        return [self._current_audio_status(_payload(row)) for row in rows]

    def get_audio_asset_media(self, audio_asset_id: UUID | str) -> Path:
        asset_key = _as_uuid(audio_asset_id)
        with self.database.read() as conn:
            row = self._get_row(conn, "audio_assets", asset_key)
            if row is None:
                raise not_found("Audio asset")
            asset = self._current_audio_status(_payload(row))
            if asset["usage_status"] != "allowed":
                raise DomainError(422, "asset_rights_blocked", "Audio rights are not current")
            path = Path(row["storage_path"]).resolve()
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                raise DomainError(422, "asset_rights_blocked", "Audio asset is unavailable or changed")
            return path

    def update_promotion_eligibility(
        self, candidate_id: UUID | str, data: Any
    ) -> JsonObject:
        candidate_key = _as_uuid(candidate_id)
        update = _dump(data)
        target_status = update["eligibility_status"]
        expected_revision = update["expected_revision"]
        if target_status not in {"unverified", MANUALLY_CONFIRMED}:
            raise DomainError(422, "validation_error", "Unknown eligibility status")
        note = update.get("eligibility_note")
        basis = update.get("confirmation_basis")
        if target_status == MANUALLY_CONFIRMED and (
            not isinstance(note, str)
            or not note.strip()
            or basis != "creator_account_backend"
        ):
            raise DomainError(
                422,
                "manual_check_required",
                "Manual confirmation requires a note and creator account backend basis",
            )
        if target_status == "unverified" and basis is not None:
            raise DomainError(
                422,
                "validation_error",
                "Unverified eligibility cannot include a confirmation basis",
            )
        with self.database.transaction() as conn:
            row = self._get_row(conn, "promotion_candidates", candidate_key)
            if row is None:
                raise not_found("Promotion candidate")
            current = _payload(row)
            current_revision = int(current.get("eligibility_revision", 1))
            if current_revision != expected_revision:
                raise conflict(
                    "revision_conflict",
                    "Promotion eligibility changed since it was loaded",
                    expected_revision=expected_revision,
                    current_revision=current_revision,
                )
            now = _utc_now()
            confirmed = target_status == MANUALLY_CONFIRMED
            updated = {
                **current,
                "eligibility_status": target_status,
                "eligibility_checked_at": now if confirmed else None,
                "eligibility_note": note.strip() if confirmed else None,
                "eligibility_revision": current_revision + 1,
                "eligibility_checked_by_user_id": self.owner_user_id if confirmed else None,
            }
            conn.execute(
                "UPDATE promotion_candidates SET eligibility_revision = ?, "
                "eligibility_checked_by_user_id = ?, confirmation_basis = ?, payload = ? "
                "WHERE id = ? AND owner_user_id = ? AND eligibility_revision = ?",
                (
                    updated["eligibility_revision"],
                    updated["eligibility_checked_by_user_id"],
                    basis if confirmed else None,
                    _json(updated),
                    candidate_key,
                    self.owner_user_id,
                    current_revision,
                ),
            )
        return updated

    def create_evidence(self, data: Any) -> JsonObject:
        evidence_data = _dump(data)
        if evidence_data.get("metric_value") is None and not evidence_data.get(
            "missing_reason"
        ):
            raise DomainError(
                422,
                "missing_reason_required",
                "Missing metric values must include a reason",
            )
        observed_at = _timestamp(evidence_data["observed_at"])
        if observed_at > datetime.now(timezone.utc):
            raise DomainError(
                422, "invalid_observed_at", "Evidence observed_at cannot be in the future"
            )
        evidence_id = str(uuid4())
        collected_at = _utc_now()
        result = {
            **evidence_data,
            "id": evidence_id,
            "owner_user_id": self.owner_user_id,
            "collected_at": collected_at,
        }
        with self.database.transaction() as conn:
            conn.execute(
                "INSERT INTO evidence(id, owner_user_id, collected_at, payload) "
                "VALUES (?, ?, ?, ?)",
                (evidence_id, self.owner_user_id, collected_at, _json(result)),
            )
        return result

    def list_evidence(
        self, *, limit: int = 20, cursor: str | None = None,
        source_type: str | None = None, q: str | None = None,
    ) -> JsonObject:
        self._validate_page_limit(limit)
        normalized_query = (q or "").strip().casefold() or None
        if normalized_query and len(normalized_query) > 100:
            raise DomainError(422, "invalid_query", "Evidence search query is too long")
        filters = {"resource": "evidence", "source_type": source_type, "q": normalized_query}
        position = self._decode_page_cursor(cursor, filters) if cursor else None
        sql = "SELECT id, collected_at AS created_at, payload FROM evidence WHERE owner_user_id = ?"
        args: list[Any] = [self.owner_user_id]
        if source_type is not None:
            sql += " AND json_extract(payload, '$.source_type') = ?"
            args.append(source_type)
        if position:
            sql += " AND (collected_at < ? OR (collected_at = ? AND id < ?))"
            args.extend([position["created_at"], position["created_at"], position["id"]])
        sql += " ORDER BY collected_at DESC, id DESC"
        with self.database.read() as conn:
            rows = conn.execute(sql, args).fetchall()
        matching: list[tuple[Any, JsonObject]] = []
        for row in rows:
            record = _payload(row)
            if normalized_query is not None:
                searchable = " ".join(
                    str(record.get(field) or "") for field in ("source_note", "source_url")
                ).casefold()
                if normalized_query not in searchable:
                    continue
            matching.append((row, record))
        page = matching[:limit]
        next_cursor = (
            self._encode_page_cursor(filters, page[-1][0])
            if len(matching) > limit and page else None
        )
        return {"items": [record for _row, record in page], "next_cursor": next_cursor}

    # --- W4-A: metric observations ---

    _WINDOWED_BASES = {
        "authorized_order_sales", "third_party_or_manual_estimate", "engagement_signal",
    }

    def create_metric_observation(self, data: Any) -> JsonObject:
        record = _dump(data)
        evidence_id = record.get("evidence_id")
        value = record.get("value")
        missing_reason = record.get("missing_reason")
        if value is None and not (missing_reason or "").strip():
            raise DomainError(422, "missing_reason_required", "Missing metric values must include a reason")
        if value is not None and evidence_id is None:
            raise DomainError(422, "validation_error", "A numeric observation requires an evidence record")
        if value is not None and missing_reason is not None:
            raise DomainError(422, "validation_error", "A valued observation must not carry a missing reason")
        if value is not None and not math.isfinite(value):
            raise DomainError(422, "validation_error", "Metric value must be finite")
        window_start = _timestamp(record["window_start"]) if record.get("window_start") else None
        window_end = _timestamp(record["window_end"]) if record.get("window_end") else None
        if (window_start is None) != (window_end is None):
            raise DomainError(422, "validation_error", "window_start and window_end must both be present or absent")
        if window_start and window_end and window_start > window_end:
            raise DomainError(422, "validation_error", "window_start must not follow window_end")
        if record["basis"] in self._WINDOWED_BASES and (window_start is None or window_end is None):
            raise DomainError(422, "validation_error", f"{record['basis']} observations require a time window")
        observed_at = _timestamp(record["observed_at"])
        if observed_at > datetime.now(timezone.utc):
            raise DomainError(422, "invalid_observed_at", "observed_at cannot be in the future")
        expires_at = _timestamp(record["expires_at"]) if record.get("expires_at") else None
        book_id = _as_uuid(record["book_id"]) if record.get("book_id") else None
        with self.database.transaction() as conn:
            if evidence_id and self._get(conn, "evidence", evidence_id) is None:
                raise not_found("Evidence")
            if book_id and self._get(conn, "books", book_id) is None:
                raise not_found("Book edition")
            observation_id = str(uuid4())
            collected_at = _utc_now()
            result = {
                **record,
                "platform": record.get("platform"),
                "book_id": book_id,
                "window_start": window_start.isoformat().replace("+00:00", "Z") if window_start else None,
                "window_end": window_end.isoformat().replace("+00:00", "Z") if window_end else None,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "expires_at": expires_at.isoformat().replace("+00:00", "Z") if expires_at else None,
                "evidence_id": _as_uuid(evidence_id) if evidence_id else None,
                "id": observation_id,
                "owner_user_id": self.owner_user_id,
                "collected_at": collected_at,
                "legacy_backfill": False,
            }
            conn.execute(
                "INSERT INTO metric_observations(id, owner_user_id, collected_at, payload) "
                "VALUES (?, ?, ?, ?)",
                (observation_id, self.owner_user_id, collected_at, _json(result)),
            )
        return result

    def get_metric_observation(self, observation_id: UUID | str) -> JsonObject:
        return self._get_or_404("metric_observations", observation_id, "Metric observation")

    def list_metric_observations(
        self, *, limit: int = 20, cursor: str | None = None, platform: str | None = None,
        object_type: str | None = None, metric_name: str | None = None,
        basis: str | None = None, book_id: UUID | str | None = None,
    ) -> JsonObject:
        self._validate_page_limit(limit)
        book_key = _as_uuid(book_id) if book_id is not None else None
        filters = {"resource": "metric_observations", "platform": platform, "object_type": object_type,
                   "metric_name": metric_name, "basis": basis, "book_id": book_key}
        position = self._decode_page_cursor(cursor, filters) if cursor else None
        sql = ("SELECT id, json_extract(payload, '$.observed_at') AS created_at, payload "
               "FROM metric_observations WHERE owner_user_id = ?")
        args: list[Any] = [self.owner_user_id]
        for field, value in (("platform", platform), ("object_type", object_type),
                             ("metric_name", metric_name), ("basis", basis), ("book_id", book_key)):
            if value is not None:
                sql += f" AND json_extract(payload, '$.{field}') = ?"
                args.append(value)
        observed = "json_extract(payload, '$.observed_at')"
        if position:
            sql += f" AND ({observed} < ? OR ({observed} = ? AND id < ?))"
            args.extend([position["created_at"], position["created_at"], position["id"]])
        sql += f" ORDER BY {observed} DESC, id DESC"
        with self.database.read() as conn:
            rows = conn.execute(sql, args).fetchall()
        page_rows = rows[: limit + 1]
        has_more = len(page_rows) > limit
        page_rows = page_rows[:limit]
        return {
            "items": [_payload(row) for row in page_rows],
            "next_cursor": self._encode_page_cursor(filters, page_rows[-1]) if has_more and page_rows else None,
        }

    # --- W4-A: content references ---

    def create_content_reference(self, data: Any) -> JsonObject:
        record = _dump(data)
        self._validate_web_uri(record.get("reference_url"), "reference_url")
        evidence_ids = [_as_uuid(item) for item in record.get("evidence_ids", [])]
        book_id = _as_uuid(record["book_id"]) if record.get("book_id") else None
        observed_at = _timestamp(record["observed_at"]) if record.get("observed_at") else None
        if record.get("material_statement") != "no_third_party_media_or_verbatim_transcript":
            raise DomainError(422, "validation_error", "Material rights declaration is required")
        with self.database.transaction() as conn:
            for evidence_id in evidence_ids:
                if self._get(conn, "evidence", evidence_id) is None:
                    raise not_found("Evidence")
            if book_id and self._get(conn, "books", book_id) is None:
                raise not_found("Book edition")
            reference_id = str(uuid4())
            created_at = _utc_now()
            result = {
                **record,
                "book_id": book_id,
                "evidence_ids": evidence_ids,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z") if observed_at else None,
                "id": reference_id,
                "owner_user_id": self.owner_user_id,
                "created_at": created_at,
            }
            conn.execute(
                "INSERT INTO content_references(id, owner_user_id, created_at, payload) "
                "VALUES (?, ?, ?, ?)",
                (reference_id, self.owner_user_id, created_at, _json(result)),
            )
        return result

    def get_content_reference(self, reference_id: UUID | str) -> JsonObject:
        return self._get_or_404("content_references", reference_id, "Content reference")

    def list_content_references(
        self, *, limit: int = 20, cursor: str | None = None, platform: str | None = None,
        ref_type: str | None = None, book_id: UUID | str | None = None,
    ) -> JsonObject:
        self._validate_page_limit(limit)
        book_key = _as_uuid(book_id) if book_id is not None else None
        filters = {"resource": "content_references", "platform": platform, "ref_type": ref_type, "book_id": book_key}
        position = self._decode_page_cursor(cursor, filters) if cursor else None
        sql = "SELECT id, created_at, payload FROM content_references WHERE owner_user_id = ?"
        args: list[Any] = [self.owner_user_id]
        for field, value in (("platform", platform), ("ref_type", ref_type), ("book_id", book_key)):
            if value is not None:
                sql += f" AND json_extract(payload, '$.{field}') = ?"
                args.append(value)
        if position:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            args.extend([position["created_at"], position["created_at"], position["id"]])
        sql += " ORDER BY created_at DESC, id DESC"
        with self.database.read() as conn:
            rows = conn.execute(sql, args).fetchall()
        page_rows = rows[: limit + 1]
        has_more = len(page_rows) > limit
        page_rows = page_rows[:limit]
        return {
            "items": [_payload(row) for row in page_rows],
            "next_cursor": self._encode_page_cursor(filters, page_rows[-1]) if has_more and page_rows else None,
        }

    # --- W4-A: style cards ---

    def _style_card_sample_count(self, card: JsonObject) -> int:
        return len({str(item) for item in card.get("evidence_ids", [])})

    def _validate_style_evidence(self, conn: Any, evidence_ids: list[str], reference_ids: list[str]) -> None:
        for evidence_id in evidence_ids:
            if self._get(conn, "evidence", evidence_id) is None:
                raise not_found("Evidence")
        for reference_id in reference_ids:
            if self._get(conn, "content_references", reference_id) is None:
                raise not_found("Content reference")

    @staticmethod
    def _validate_style_features(features: list[JsonObject], evidence_ids: list[str]) -> None:
        evidence_set = {str(item) for item in evidence_ids}
        for feature in features:
            cited = {str(item) for item in feature.get("evidence_ids", [])}
            if not cited or not cited <= evidence_set:
                raise DomainError(422, "validation_error", "Every feature must cite evidence on its card")
            if int(feature.get("sample_count", 0)) != len(cited):
                raise DomainError(422, "validation_error", "Feature sample count must equal distinct cited evidence")

    def create_style_card(self, data: Any) -> JsonObject:
        record = _dump(data)
        book_id = _as_uuid(record["book_id"]) if record.get("book_id") else None
        derived_by = record.get("derived_by") or "manual"
        with self.database.transaction() as conn:
            self._validate_style_evidence(conn, record.get("evidence_ids", []), record.get("reference_ids", []))
            if book_id and self._get(conn, "books", book_id) is None:
                raise not_found("Book edition")
            evidence_ids = [_as_uuid(item) for item in record.get("evidence_ids", [])]
            reference_ids = [_as_uuid(item) for item in record.get("reference_ids", [])]

            if derived_by == "workflow_extraction":
                # Features for workflow-derived cards are recomputed server-side
                # from the owner-scoped evidence and references; client-supplied
                # feature lists are never trusted as extraction output.
                evidence_records: list[JsonObject] = []
                for evidence_id in evidence_ids:
                    record_row = self._get(conn, "evidence", evidence_id)
                    if record_row is not None:
                        evidence_records.append({**record_row, "kind": "evidence"})
                reference_records: list[JsonObject] = []
                for reference_id in reference_ids:
                    record_row = self._get(conn, "content_references", reference_id)
                    if record_row is not None:
                        reference_records.append({**record_row, "kind": "reference"})
                features, extracted_evidence, extracted_references = extract_style_features(
                    evidence_records + reference_records,
                )
                evidence_ids = [_as_uuid(item) for item in extracted_evidence]
                reference_ids = [_as_uuid(item) for item in extracted_references]
            else:
                features = record.get("features", [])
            self._validate_style_features(features, evidence_ids)

            card_id = str(uuid4())
            now = _utc_now()
            has_evidence = bool(evidence_ids) and bool(features)
            result = {
                **record,
                "derived_by": derived_by,
                "book_id": book_id,
                "features": features,
                "evidence_ids": evidence_ids,
                "reference_ids": reference_ids,
                "id": card_id,
                "owner_user_id": self.owner_user_id,
                "created_at": now,
                "updated_at": now,
                "revision": 1,
                "review_status": "pending_review" if has_evidence else "draft",
                "edited_by_user_id": None,
                "last_edited_at": None,
                "abstract_only": True,
                "evidence_sample_count": self._style_card_sample_count({"evidence_ids": evidence_ids}),
            }
            conn.execute(
                "INSERT INTO style_cards(id, owner_user_id, created_at, payload) VALUES (?, ?, ?, ?)",
                (card_id, self.owner_user_id, now, _json(result)),
            )
        return result

    def get_style_card(self, style_card_id: UUID | str) -> JsonObject:
        return self._get_or_404("style_cards", style_card_id, "Style card")

    def list_style_cards(
        self, *, limit: int = 20, cursor: str | None = None,
        review_status: str | None = None, target_platform: str | None = None,
        book_id: UUID | str | None = None,
    ) -> JsonObject:
        self._validate_page_limit(limit)
        book_key = _as_uuid(book_id) if book_id is not None else None
        filters = {"resource": "style_cards", "review_status": review_status,
                   "target_platform": target_platform, "book_id": book_key}
        position = self._decode_page_cursor(cursor, filters) if cursor else None
        sql = "SELECT id, created_at, payload FROM style_cards WHERE owner_user_id = ?"
        args: list[Any] = [self.owner_user_id]
        for field, value in (("review_status", review_status), ("target_platform", target_platform), ("book_id", book_key)):
            if value is not None:
                sql += f" AND json_extract(payload, '$.{field}') = ?"
                args.append(value)
        if position:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            args.extend([position["created_at"], position["created_at"], position["id"]])
        sql += " ORDER BY created_at DESC, id DESC"
        with self.database.read() as conn:
            rows = conn.execute(sql, args).fetchall()
        page_rows = rows[: limit + 1]
        has_more = len(page_rows) > limit
        page_rows = page_rows[:limit]
        return {
            "items": [_payload(row) for row in page_rows],
            "next_cursor": self._encode_page_cursor(filters, page_rows[-1]) if has_more and page_rows else None,
        }

    def update_style_card(self, style_card_id: UUID | str, data: Any) -> JsonObject:
        card_key = _as_uuid(style_card_id)
        update = _dump(data)
        expected_revision = update["expected_revision"]
        with self.database.transaction() as conn:
            row = self._get_row(conn, "style_cards", card_key)
            if row is None:
                raise not_found("Style card")
            current = _payload(row)
            if int(current["revision"]) != expected_revision:
                raise conflict("revision_conflict", "Style card changed since it was loaded",
                              expected_revision=expected_revision, current_revision=current["revision"])
            evidence_ids = (
                [_as_uuid(item) for item in update["evidence_ids"]]
                if update.get("evidence_ids") is not None else list(current["evidence_ids"])
            )
            reference_ids = (
                [_as_uuid(item) for item in update["reference_ids"]]
                if update.get("reference_ids") is not None else list(current["reference_ids"])
            )
            features = update["features"] if update.get("features") is not None else current.get("features", [])
            self._validate_style_features(features, evidence_ids)
            self._validate_style_evidence(conn, evidence_ids, reference_ids)
            now = _utc_now()
            result = {
                **current,
                "title": update.get("title", current["title"]),
                "target_platform": update.get("target_platform", current.get("target_platform")),
                "features": features,
                "evidence_ids": evidence_ids,
                "reference_ids": reference_ids,
                "human_note": update.get("human_note", current.get("human_note")),
                "revision": int(current["revision"]) + 1,
                "updated_at": now,
                "edited_by_user_id": self.owner_user_id,
                "last_edited_at": now,
                "evidence_sample_count": self._style_card_sample_count({"evidence_ids": evidence_ids}),
            }
            if current["review_status"] == "approved":
                # Human revision after approval returns the card to pending review.
                result["review_status"] = "pending_review"
            conn.execute(
                "UPDATE style_cards SET payload = ? WHERE id = ? AND owner_user_id = ?",
                (_json(result), card_key, self.owner_user_id),
            )
        return result

    def review_style_card(self, style_card_id: UUID | str, data: Any) -> JsonObject:
        card_key = _as_uuid(style_card_id)
        review = _dump(data)
        decision = review["decision"]
        note = review.get("note")
        if decision == "reject" and not (note or "").strip():
            raise DomainError(422, "validation_error", "A rejected style card requires a note")
        with self.database.transaction() as conn:
            row = self._get_row(conn, "style_cards", card_key)
            if row is None:
                raise not_found("Style card")
            current = _payload(row)
            if decision == "approve":
                if not current.get("evidence_ids") or not current.get("features"):
                    raise DomainError(422, "validation_error", "Approval requires at least one evidence-backed feature")
                self._validate_style_evidence(conn, current["evidence_ids"], current.get("reference_ids", []))
                self._validate_style_features(current["features"], current["evidence_ids"])
            now = _utc_now()
            result = {
                **current,
                "review_status": "approved" if decision == "approve" else "rejected",
                "human_note": note if note is not None else current.get("human_note"),
                "updated_at": now,
                "edited_by_user_id": self.owner_user_id,
                "last_edited_at": now,
            }
            conn.execute(
                "UPDATE style_cards SET payload = ? WHERE id = ? AND owner_user_id = ?",
                (_json(result), card_key, self.owner_user_id),
            )
        return result

    def create_workflow_style_card(
        self, *, title: str | None = None, book_id: UUID | str | None = None,
        evidence_ids: list[UUID | str] | None = None,
        reference_ids: list[UUID | str] | None = None,
    ) -> JsonObject:
        """Build an evidence-backed style card from the extraction workflow.

        The card always starts as a draft or pending review; a human must approve
        it before it can influence script generation.
        """
        evidence_keys = [_as_uuid(item) for item in (evidence_ids or [])]
        reference_keys = [_as_uuid(item) for item in (reference_ids or [])]
        book_key = _as_uuid(book_id) if book_id else None
        with self.database.transaction() as conn:
            evidence_records: list[JsonObject] = []
            for evidence_id in evidence_keys:
                record = self._get(conn, "evidence", evidence_id)
                if record is None:
                    raise not_found("Evidence")
                evidence_records.append({**record, "kind": "evidence"})
            reference_records: list[JsonObject] = []
            for reference_id in reference_keys:
                record = self._get(conn, "content_references", reference_id)
                if record is None:
                    raise not_found("Content reference")
                reference_records.append({**record, "kind": "reference"})
            if book_key and self._get(conn, "books", book_key) is None:
                raise not_found("Book edition")

        features, extracted_evidence, extracted_references = extract_style_features(
            evidence_records + reference_records,
        )
        auto_title = (title or "").strip() or "证据提炼的抽象风格（待人工审核）"
        payload = {
            "title": auto_title,
            "target_platform": None,
            "book_id": book_key,
            "derived_by": "workflow_extraction",
            "features": features,
            "evidence_ids": extracted_evidence,
            "reference_ids": extracted_references,
            "human_note": "由证据与内容参考自动提炼；特征为抽象模式，需人工核对后才能作为创作参考。",
        }
        return self.create_style_card(payload)

    def list_approved_style_guidance(self, book_id: UUID | str | None = None) -> list[dict[str, Any]]:
        """Approved, abstract style hints available to script generation."""
        book_key = _as_uuid(book_id) if book_id else None
        with self.database.read() as conn:
            rows = conn.execute(
                "SELECT payload FROM style_cards WHERE owner_user_id = ? "
                "AND json_extract(payload, '$.review_status') = 'approved'",
                (self.owner_user_id,),
            ).fetchall()
        guidance: list[dict[str, Any]] = []
        for row in rows:
            card = _payload(row)
            if book_key and card.get("book_id") and card["book_id"] != book_key:
                continue
            guidance.extend(style_guidance(card.get("features", [])))
        # De-duplicate by feature key, keeping the highest sample count.
        merged: dict[str, dict[str, Any]] = {}
        for item in guidance:
            existing = merged.get(item["feature_key"])
            if existing is None or item["sample_count"] > existing["sample_count"]:
                merged[item["feature_key"]] = item
        return sorted(merged.values(), key=lambda item: item["feature_key"])

    @staticmethod
    def _validate_web_uri(value: str | None, field_name: str) -> None:
        if value is None:
            return
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DomainError(422, "validation_error", f"{field_name} must be an absolute http(s) URI")

    # --- W4-A: CSV import jobs ---

    PREVIEW_TTL_SECONDS = 3600

    _PREVIEW_FIELDS = {
        "id", "owner_user_id", "status", "filename", "encoding_detected", "file_sha256",
        "size_bytes", "columns", "total_rows", "parsed_rows", "valid_rows", "failed_rows",
        "skipped_duplicate_rows", "mapping_suggestions", "row_diagnostics", "created_at",
        "expires_at",
    }
    _JOB_FIELDS = {
        "id", "owner_user_id", "status", "filename", "file_sha256", "created_at",
        "updated_at", "counts", "row_diagnostics", "entity_kinds", "mapping",
        "on_duplicate", "match_by",
    }

    @classmethod
    def _preview_response(cls, payload: JsonObject) -> JsonObject:
        return {k: v for k, v in payload.items() if k in cls._PREVIEW_FIELDS}

    @classmethod
    def _job_response(cls, payload: JsonObject) -> JsonObject:
        return {k: v for k, v in payload.items() if k in cls._JOB_FIELDS}

    def preview_import(
        self, content: bytes, *, filename_hint: str | None, idempotency_key: str
    ) -> JsonObject:
        self._validate_idempotency_key(idempotency_key)
        if len(content) > 1024 * 1024:
            raise DomainError(422, "import_file_too_large", "CSV file exceeds the 1 MiB size limit")
        file_hash = hashlib.sha256(content).hexdigest()
        filename = (filename_hint or "import.csv").strip()[:200] or "import.csv"
        operation = "import_preview"
        request_hash = _request_hash({"filename": filename, "file_sha256": file_hash})
        with self.database.transaction() as conn:
            replay = self._get_idempotent_mutation(conn, idempotency_key, operation, file_hash, request_hash)
            if replay is not None:
                return self._preview_response(replay)
            parsed = import_csv.parse_csv(content, sha256=file_hash)
            if len(parsed.rows) > import_csv.MAX_ROWS:
                raise DomainError(422, "import_too_many_rows", f"CSV exceeds the {import_csv.MAX_ROWS}-row limit")
            if parsed.encoding_failed:
                diagnostics = [d.as_dict() for d in parsed.parse_diagnostics]
                job_id = str(uuid4())
                now = _utc_now()
                result = {
                    "id": job_id, "owner_user_id": self.owner_user_id, "status": "failed",
                    "filename": filename, "file_sha256": file_hash, "created_at": now, "updated_at": now,
                    "counts": {"total": 0, "parsed": 0, "persisted": 0, "skipped_duplicate": 0,
                               "failed": 0, "warnings": 0},
                    "total_rows": 0, "parsed_rows": 0, "valid_rows": 0, "failed_rows": 0,
                    "skipped_duplicate_rows": 0,
                    "row_diagnostics": diagnostics, "entity_kinds": [], "mapping": {},
                    "on_duplicate": "skip", "match_by": "isbn_and_edition",
                    "encoding_detected": parsed.encoding_detected, "size_bytes": parsed.size_bytes,
                    "columns": parsed.columns, "mapping_suggestions": {},
                    "expires_at": now, "draft": None,
                }
                conn.execute(
                    "INSERT INTO import_jobs(id, owner_user_id, status, file_sha256, created_at, updated_at, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (job_id, self.owner_user_id, "failed", file_hash, now, now, _json(result)),
                )
                self._store_idempotent_mutation(conn, idempotency_key, operation, file_hash, request_hash, result)
                return self._preview_response(result)

            suggestions = import_csv.suggest_mapping(parsed.columns)
            _records, row_diagnostics, counts = import_csv.validate_rows(
                parsed.rows, suggestions, match_by="isbn_and_edition", do_normalize_isbn=True,
            )
            job_id = str(uuid4())
            now = _utc_now()
            expires = (datetime.now(timezone.utc) + timedelta(seconds=self.PREVIEW_TTL_SECONDS))
            expires_at = expires.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            result = {
                "id": job_id, "owner_user_id": self.owner_user_id, "status": "preview",
                "filename": filename, "file_sha256": file_hash, "created_at": now, "updated_at": now,
                "counts": {**counts, "persisted": 0},
                "total_rows": counts["total"], "parsed_rows": counts["parsed"],
                "valid_rows": counts["parsed"] - counts["failed"] - counts["skipped_duplicate"],
                "failed_rows": counts["failed"],
                "skipped_duplicate_rows": counts["skipped_duplicate"],
                "row_diagnostics": [d.as_dict() for d in row_diagnostics],
                "entity_kinds": [], "mapping": suggestions,
                "on_duplicate": "skip", "match_by": "isbn_and_edition",
                "encoding_detected": parsed.encoding_detected, "size_bytes": parsed.size_bytes,
                "columns": parsed.columns, "mapping_suggestions": suggestions,
                "expires_at": expires_at,
                "draft": {"rows": parsed.rows, "parsed_count": len(parsed.rows)},
            }
            conn.execute(
                "INSERT INTO import_jobs(id, owner_user_id, status, file_sha256, created_at, updated_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job_id, self.owner_user_id, "preview", file_hash, now, now, _json(result)),
            )
            self._store_idempotent_mutation(conn, idempotency_key, operation, file_hash, request_hash, result)
        return self._preview_response(result)

    def get_import(self, import_id: UUID | str) -> JsonObject:
        job = self._get_or_404("import_jobs", import_id, "Import job")
        return self._job_response(job)

    def list_imports(
        self, *, limit: int = 20, cursor: str | None = None, status: str | None = None
    ) -> JsonObject:
        self._validate_page_limit(limit)
        filters = {"resource": "import_jobs", "status": status}
        position = self._decode_page_cursor(cursor, filters) if cursor else None
        sql = "SELECT id, created_at, payload FROM import_jobs WHERE owner_user_id = ?"
        args: list[Any] = [self.owner_user_id]
        if status is not None:
            sql += " AND json_extract(payload, '$.status') = ?"
            args.append(status)
        if position:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            args.extend([position["created_at"], position["created_at"], position["id"]])
        sql += " ORDER BY created_at DESC, id DESC"
        with self.database.read() as conn:
            rows = conn.execute(sql, args).fetchall()
        page_rows = rows[: limit + 1]
        has_more = len(page_rows) > limit
        page_rows = page_rows[:limit]
        items = [self._job_response(_payload(row)) for row in page_rows]
        return {
            "items": items,
            "next_cursor": self._encode_page_cursor(filters, page_rows[-1]) if has_more and page_rows else None,
        }

    def confirm_import(
        self, import_id: UUID | str, data: Any, idempotency_key: str
    ) -> JsonObject:
        job_key = _as_uuid(import_id)
        request = _dump(data)
        self._validate_idempotency_key(idempotency_key)
        operation = "import_confirm"
        request_hash = _request_hash({"import_id": job_key, **request})
        with self.database.transaction() as conn:
            replay = self._get_idempotent_mutation(
                conn, idempotency_key, operation, job_key, request_hash
            )
            if replay is not None:
                return self._job_response(replay)
            row = self._get_row(conn, "import_jobs", job_key)
            if row is None:
                raise not_found("Import job")
            draft = _payload(row)
            if draft["status"] != "preview":
                raise conflict("import_already_applied", "Import job has already been applied")
            if draft.get("expires_at") and _timestamp(draft["expires_at"]) <= datetime.now(timezone.utc):
                raise DomainError(422, "import_preview_expired", "Preview draft has expired; upload the file again")
            mapping = request.get("mapping") or {}
            unknown_sources = set(mapping) - set(draft.get("columns", []))
            if unknown_sources:
                raise DomainError(
                    422, "import_mapping_invalid", "Mapping references unknown CSV columns",
                    details={"columns": sorted(unknown_sources)},
                )
            unknown_targets = {
                target for target in mapping.values()
                if target is not None and target not in import_csv.VALID_MAPPING_TARGETS
            }
            if unknown_targets:
                raise DomainError(422, "import_mapping_invalid", "Mapping references unknown target fields",
                                  details={"targets": sorted(unknown_targets)})
            chosen_targets = [target for target in mapping.values() if target is not None]
            if len(chosen_targets) != len(set(chosen_targets)):
                raise DomainError(422, "import_mapping_invalid", "Each target field may be mapped once")
            confirm = {
                "on_duplicate": request.get("on_duplicate", "skip"),
                "match_by": request.get("match_by", "isbn_and_edition"),
                "normalize_isbn": request.get("normalize_isbn", True),
            }
            draft_rows = (draft.get("draft") or {}).get("rows", [])
            records, diagnostics, counts = import_csv.validate_rows(
                draft_rows, mapping, match_by=confirm["match_by"], do_normalize_isbn=confirm["normalize_isbn"],
            )
            persisted: list[str] = []
            entity_kinds: set[str] = set()

            for record in records:
                if record["error_count"] > 0:
                    continue
                draft_book = record["book"]
                book_id: str | None = None
                if draft_book.get("title") or draft_book.get("isbn"):
                    book_id = self._import_upsert_book(conn, draft_book, confirm)
                    if book_id:
                        entity_kinds.add("book_edition")
                if record["has_candidate"] and book_id and record["candidate"].get("platform"):
                    candidate_id = self._import_upsert_candidate(conn, book_id, record["candidate"], confirm)
                    if candidate_id:
                        entity_kinds.add("promotion_candidate")
                if record["has_reference"] and record["reference"].get("title"):
                    reference_id = self._import_create_reference(conn, record["reference"], book_id)
                    if reference_id:
                        entity_kinds.add("content_reference")
                if record["has_metric"]:
                    metric_id = self._import_create_metric(
                        conn, record["metric"], book_id, draft["file_sha256"]
                    )
                    if metric_id:
                        entity_kinds.add("metric_observation")
                        if record["metric"].get("value") is not None:
                            entity_kinds.add("evidence")

            failed = sum(1 for r in records if r["error_count"] > 0)
            persisted_count = len(records) - failed - counts["skipped_duplicate"]
            warnings = sum(1 for d in diagnostics if d.severity == "warning")
            if failed == 0:
                status = "applied"
            elif persisted_count > 0:
                status = "partially_applied"
            else:
                status = "failed"
            now = _utc_now()
            result = {
                **draft,
                "status": status,
                "updated_at": now,
                "counts": {"total": counts["total"], "parsed": counts["parsed"],
                           "persisted": max(persisted_count, 0),
                           "skipped_duplicate": counts["skipped_duplicate"], "failed": failed,
                           "warnings": warnings},
                "row_diagnostics": [d.as_dict() for d in diagnostics],
                "entity_kinds": sorted(entity_kinds),
                "mapping": mapping,
                "on_duplicate": confirm["on_duplicate"],
                "match_by": confirm["match_by"],
            }
            conn.execute(
                "UPDATE import_jobs SET status = ?, file_sha256 = ?, updated_at = ?, payload = ? "
                "WHERE id = ? AND owner_user_id = ?",
                (status, draft["file_sha256"], now, _json(result), job_key, self.owner_user_id),
            )
            self._store_idempotent_mutation(conn, idempotency_key, operation, job_key, request_hash, result)
        return self._job_response(result)

    def _import_upsert_book(
        self, conn: Any, draft: JsonObject, confirm: JsonObject
    ) -> str | None:
        isbn = import_csv.normalize_isbn(draft.get("isbn")) if confirm["normalize_isbn"] else draft.get("isbn")
        title = (draft.get("title") or "").strip()
        edition = (draft.get("edition_label") or "").strip()
        author = (draft.get("author") or "").strip()
        if not title:
            return None
        existing = conn.execute(
            "SELECT id, payload FROM books WHERE owner_user_id = ?", (self.owner_user_id,)
        ).fetchall()
        for row in existing:
            book = _payload(row)
            match = False
            if confirm["match_by"] == "isbn" and isbn:
                match = (book.get("isbn") or "").replace("-", "").upper() == isbn
            elif confirm["match_by"] == "title_author_fuzzy" or not isbn:
                match = (book.get("title", "").casefold() == title.casefold()
                         and book.get("author", "").casefold() == author.casefold()
                         and book.get("edition_label", "").casefold() == edition.casefold())
            else:
                match = ((book.get("isbn") or "").replace("-", "").upper() == isbn
                         and book.get("edition_label", "").casefold() == edition.casefold())
            if match:
                if edition and (book.get("edition_label") or "").casefold() != edition.casefold():
                    raise DomainError(
                        422, "edition_conflict", "Matched ISBN belongs to a different edition",
                    )
                if confirm["on_duplicate"] == "update":
                    updated = {**book, "title": title, "edition_label": edition or book.get("edition_label"),
                               "author": author or book.get("author"),
                               "publisher": draft.get("publisher") or book.get("publisher")}
                    conn.execute("UPDATE books SET payload = ? WHERE id = ? AND owner_user_id = ?",
                                 (_json(updated), row["id"], self.owner_user_id))
                return row["id"]
        book_id = str(uuid4())
        now = _utc_now()
        opinion = (draft.get("original_opinion") or "").strip() or "CSV 导入的图书，待创作者补充原创观点。"
        payload = {
            "id": book_id, "owner_user_id": self.owner_user_id, "created_at": now,
            "title": title, "edition_label": edition or "未注明版次", "author": author or "佚名",
            "isbn": isbn, "publisher": draft.get("publisher") or None,
            "original_opinion": opinion, "facts": [],
        }
        conn.execute(
            "INSERT INTO books(id, owner_user_id, created_at, payload) VALUES (?, ?, ?, ?)",
            (book_id, self.owner_user_id, now, _json(payload)),
        )
        return book_id

    def _import_upsert_candidate(
        self, conn: Any, book_id: str, draft: JsonObject, confirm: JsonObject
    ) -> str | None:
        product_id = draft.get("platform_product_id")
        product_url = draft.get("product_url")
        platform = draft.get("platform")
        if not product_id or not product_url or platform not in import_csv.PLATFORMS:
            return None
        existing = conn.execute(
            "SELECT id, payload FROM promotion_candidates WHERE owner_user_id = ? AND book_id = ?",
            (self.owner_user_id, book_id),
        ).fetchall()
        for row in existing:
            candidate = _payload(row)
            if candidate.get("platform_product_id") == product_id and candidate.get("platform") == platform:
                return row["id"]
        candidate_id = str(uuid4())
        now = _utc_now()
        payload = {
            "id": candidate_id, "owner_user_id": self.owner_user_id, "created_at": now,
            "book_id": book_id, "platform": platform, "platform_product_id": product_id,
            "product_url": product_url, "eligibility_status": "unverified",
            "eligibility_checked_at": None, "eligibility_note": None,
            "eligibility_revision": 1, "eligibility_checked_by_user_id": None,
        }
        conn.execute(
            "INSERT INTO promotion_candidates(id, owner_user_id, book_id, created_at, eligibility_revision, "
            "eligibility_checked_by_user_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, self.owner_user_id, book_id, now, 1, None, _json(payload)),
        )
        return candidate_id

    def _import_create_reference(
        self, conn: Any, draft: JsonObject, book_id: str | None
    ) -> str | None:
        title = draft.get("title")
        if not title:
            return None
        url = draft.get("reference_url")
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return None
        reference_id = str(uuid4())
        now = _utc_now()
        payload = {
            "id": reference_id, "owner_user_id": self.owner_user_id, "created_at": now,
            "platform": None, "book_id": book_id, "ref_type": draft.get("ref_type") or "content_item",
            "external_ref": draft.get("external_ref"), "reference_url": url,
            "title": title, "note": draft.get("note"),
            "license_scope": draft.get("license_scope") or "manual_observation",
            "usage_basis": draft.get("usage_basis") or "人工观察记录，仅作研究参考",
            "captured_fields": ["title", "observed_note"], "evidence_ids": [],
            "observed_at": None,
            "material_statement": "no_third_party_media_or_verbatim_transcript",
        }
        conn.execute(
            "INSERT INTO content_references(id, owner_user_id, created_at, payload) VALUES (?, ?, ?, ?)",
            (reference_id, self.owner_user_id, now, _json(payload)),
        )
        return reference_id

    def _import_create_metric(
        self, conn: Any, draft: JsonObject, book_id: str | None, file_sha256: str
    ) -> str | None:
        observed_at = draft.get("observed_at") or _utc_now()
        value = draft.get("value")
        missing_reason = draft.get("missing_reason")
        basis = draft.get("basis") or "legacy_unclassified"
        if basis in self._WINDOWED_BASES and not (draft.get("window_start") and draft.get("window_end")):
            # A windowed basis without a bounded window cannot be trusted as classified;
            # fall back to the unclassified bucket until a human re-validates it.
            basis = "legacy_unclassified"
        evidence_id: str | None = None
        if value is not None:
            evidence_id = self._import_backing_evidence(conn, observed_at, file_sha256)
        metric_id = str(uuid4())
        collected_at = _utc_now()
        payload = {
            "id": metric_id, "owner_user_id": self.owner_user_id, "collected_at": collected_at,
            "platform": draft.get("platform"), "book_id": book_id,
            "object_type": draft.get("object_type") or "book_edition",
            "object_ref": draft.get("object_ref") or f"import:{file_sha256[:12]}:{metric_id[:8]}",
            "object_label": draft.get("object_label"),
            "metric_name": draft.get("metric_name") or "other",
            "metric_definition": draft.get("metric_definition") or "CSV 导入指标，口径待人工核实",
            "basis": basis,
            "unit": draft.get("unit") or "unspecified",
            "value": value, "value_text": draft.get("value_text"),
            "window_start": draft.get("window_start"), "window_end": draft.get("window_end"),
            "observed_at": observed_at, "evidence_id": evidence_id,
            "missing_reason": missing_reason, "confidence": None, "expires_at": None,
            "note": draft.get("note"), "legacy_backfill": False,
        }
        conn.execute(
            "INSERT INTO metric_observations(id, owner_user_id, collected_at, payload) VALUES (?, ?, ?, ?)",
            (metric_id, self.owner_user_id, collected_at, _json(payload)),
        )
        return metric_id

    def _import_backing_evidence(self, conn: Any, observed_at: str, file_sha256: str) -> str:
        evidence_id = str(uuid4())
        collected_at = _utc_now()
        payload = {
            "id": evidence_id, "owner_user_id": self.owner_user_id, "collected_at": collected_at,
            "source_type": "manual",
            "source_note": f"CSV 导入自动生成的来源记录（文件 {file_sha256[:12]}）",
            "source_url": None, "observed_at": observed_at,
            "usage_basis": "人工 CSV 导入记录，仅作研究参考；口径待人工核实",
            "metric_name": None, "metric_value": None,
            "metric_window_start": None, "metric_window_end": None,
            "missing_reason": "CSV 导入来源记录；指标见关联的 MetricObservation",
        }
        conn.execute(
            "INSERT INTO evidence(id, owner_user_id, collected_at, payload) VALUES (?, ?, ?, ?)",
            (evidence_id, self.owner_user_id, collected_at, _json(payload)),
        )
        return evidence_id

    def create_creative_project(self, data: Any) -> JsonObject:
        project_data = _dump(data)
        book_id = _as_uuid(project_data["book_id"])
        candidate_id = _as_uuid(project_data["promotion_candidate_id"])
        evidence_ids = [_as_uuid(item) for item in project_data.get("evidence_ids", [])]
        with self.database.transaction() as conn:
            book = self._get(conn, "books", book_id)
            candidate = self._get(conn, "promotion_candidates", candidate_id)
            if book is None:
                raise not_found("Book edition")
            if candidate is None:
                raise not_found("Promotion candidate")
            if candidate["book_id"] != book_id:
                raise conflict(
                    "product_book_mismatch",
                    "Promotion candidate belongs to a different book edition",
                )
            if candidate["platform"] != project_data["target_platform"]:
                raise conflict(
                    "platform_mismatch",
                    "Creative project platform must match the promotion candidate",
                )
            for evidence_id in evidence_ids:
                if self._get(conn, "evidence", evidence_id) is None:
                    raise not_found("Evidence")
            project_id = str(uuid4())
            created_at = _utc_now()
            result = {
                **project_data,
                "book_id": book_id,
                "promotion_candidate_id": candidate_id,
                "evidence_ids": evidence_ids,
                "id": project_id,
                "owner_user_id": self.owner_user_id,
                "created_at": created_at,
            }
            conn.execute(
                "INSERT INTO creative_projects"
                "(id, owner_user_id, book_id, promotion_candidate_id, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    project_id,
                    self.owner_user_id,
                    book_id,
                    candidate_id,
                    created_at,
                    _json(result),
                ),
            )
        return result

    def get_creative_project(self, project_id: UUID | str) -> JsonObject:
        return self._get_or_404("creative_projects", project_id, "Creative project")

    def get_creative_workspace(self, project_id: UUID | str) -> JsonObject:
        project_key = _as_uuid(project_id)
        with self.database.read() as conn:
            project = self._get(conn, "creative_projects", project_key)
            if project is None:
                raise not_found("Creative project")
            book = self._get(conn, "books", project["book_id"])
            candidate = self._get(
                conn, "promotion_candidates", project["promotion_candidate_id"]
            )
            if book is None or candidate is None:
                raise conflict(
                    "project_references_missing", "Project references are unavailable"
                )
            evidence_ids = list(project.get("evidence_ids", []))
            evidence_ids.extend(
                fact["evidence_id"]
                for fact in book.get("facts", [])
                if fact.get("evidence_id")
            )
            evidence: list[JsonObject] = []
            for evidence_id in dict.fromkeys(evidence_ids):
                record = self._get(conn, "evidence", evidence_id)
                if record is not None:
                    evidence.append(record)
            scripts = conn.execute(
                "SELECT payload FROM script_versions WHERE project_id = ? "
                "AND owner_user_id = ? ORDER BY version_number, id",
                (project_key, self.owner_user_id),
            ).fetchall()
            runs = conn.execute(
                "SELECT payload FROM runs WHERE project_id = ? AND owner_user_id = ? "
                "ORDER BY created_at DESC, id DESC",
                (project_key, self.owner_user_id),
            ).fetchall()
            renders = conn.execute(
                "SELECT payload FROM render_jobs WHERE project_id = ? AND owner_user_id = ? "
                "ORDER BY created_at DESC, id DESC",
                (project_key, self.owner_user_id),
            ).fetchall()
            package_row = conn.execute(
                "SELECT payload, render_id FROM publication_packages WHERE project_id = ? "
                "AND owner_user_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (project_key, self.owner_user_id),
            ).fetchone()
            package = None
            if package_row is not None:
                render_row = self._get_row(conn, "render_jobs", package_row["render_id"])
                if render_row is not None:
                    package = self._refresh_publication_checklist(
                        conn, _payload(package_row), render_row
                    )
        return {
            "project": project,
            "book": book,
            "promotion_candidate": candidate,
            "evidence": evidence,
            "script_versions": [_payload(row) for row in scripts],
            "runs": [_payload(row) for row in runs],
            "render_jobs": [_payload(row) for row in renders],
            "publication_package": package,
        }

    def list_script_versions(self, project_id: UUID | str) -> list[JsonObject]:
        project_key = _as_uuid(project_id)
        with self.database.read() as conn:
            if self._get(conn, "creative_projects", project_key) is None:
                raise not_found("Creative project")
            rows = conn.execute(
                "SELECT payload FROM script_versions WHERE project_id = ? "
                "AND owner_user_id = ? ORDER BY version_number",
                (project_key, self.owner_user_id),
            ).fetchall()
        return [_payload(row) for row in rows]

    def save_script_version(self, project_id: UUID | str, data: Any) -> JsonObject:
        project_key = _as_uuid(project_id)
        script_data = _dump(data)
        based_on_id = script_data.get("based_on_version_id")
        with self.database.transaction() as conn:
            project = self._get(conn, "creative_projects", project_key)
            if project is None:
                raise not_found("Creative project")
            book = self._get(conn, "books", project["book_id"])
            self._validate_script_content(conn, project, book, script_data)
            if based_on_id:
                base = self._get(conn, "script_versions", based_on_id)
                if base is None or base["project_id"] != project_key:
                    raise conflict(
                        "invalid_base_version",
                        "Base script version must belong to this project",
                    )
            version_number = conn.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM script_versions "
                "WHERE project_id = ? AND owner_user_id = ?",
                (project_key, self.owner_user_id),
            ).fetchone()[0]
            script_id = str(uuid4())
            created_at = _utc_now()
            result = {
                **script_data,
                "based_on_version_id": based_on_id,
                "generation_result_id": None,
                "fact_revision_refs": self._fact_revision_refs(book, script_data),
                "id": script_id,
                "project_id": project_key,
                "owner_user_id": self.owner_user_id,
                "version_number": version_number,
                "review_status": "pending",
                "created_at": created_at,
            }
            conn.execute(
                "INSERT INTO script_versions"
                "(id, owner_user_id, project_id, version_number, review_status, created_at, payload) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (
                    script_id,
                    self.owner_user_id,
                    project_key,
                    version_number,
                    created_at,
                    _json(result),
                ),
            )
        return result

    def persist_generated_script_version(
        self, project_id: UUID | str, data: Any
    ) -> JsonObject:
        """Idempotently persist a generated ScriptVersion with its stable ID/version.

        Generated script content is immutable once stored. Replayed snapshots with the
        same identity and content return the existing row. A review-status change is
        accepted only when `review_run` already persisted the matching human decision.
        """

        project_key = _as_uuid(project_id)
        generated = _dump(data)
        script_key = _as_uuid(generated.get("id"))
        version_number = generated.get("version_number")
        if not isinstance(version_number, int) or version_number < 1:
            raise DomainError(
                422, "invalid_script_version", "Generated script version_number must be positive"
            )
        if generated.get("project_id") and _as_uuid(generated["project_id"]) != project_key:
            raise conflict(
                "script_project_mismatch",
                "Generated script must belong to the requested project",
            )
        if generated.get("owner_user_id") and _as_uuid(
            generated["owner_user_id"]
        ) != self.owner_user_id:
            raise conflict(
                "script_owner_mismatch",
                "Generated script owner is assigned by the server",
            )
        requested_status = generated.get("review_status", "pending")
        if requested_status not in {"pending", "approved", "rejected"}:
            raise DomainError(422, "invalid_review_status", "Unknown script review status")
        content_data = {
            "based_on_version_id": generated.get("based_on_version_id"),
            "segments": generated.get("segments"),
            "title_suggestion": generated.get("title_suggestion"),
            "description_suggestion": generated.get("description_suggestion"),
        }
        try:
            content = _dump(ScriptVersionCreate.model_validate(content_data))
        except Exception as exc:
            raise DomainError(
                422,
                "invalid_script_version",
                "Generated script does not match the ScriptVersion contract",
            ) from exc
        based_on_id = content.get("based_on_version_id")
        created_at = _utc_now()
        if generated.get("created_at") is not None:
            created_at = _timestamp(generated["created_at"]).isoformat().replace("+00:00", "Z")

        with self.database.transaction() as conn:
            project = self._get(conn, "creative_projects", project_key)
            if project is None:
                raise not_found("Creative project")
            book = self._get(conn, "books", project["book_id"])
            self._validate_script_content(conn, project, book, content)
            generation_result_id = generated.get("generation_result_id")
            if generation_result_id:
                generation = self._get(conn, "generation_results", generation_result_id)
                if generation is None or generation.get("script_version_id") != script_key:
                    raise conflict("generation_result_mismatch", "Generated script must reference its own result")
                source_run = self._get(conn, "runs", generation["run_id"])
                if source_run is None or source_run["project_id"] != project_key:
                    raise conflict("generation_result_mismatch", "Generation result belongs to another project")
            if based_on_id:
                base = self._get(conn, "script_versions", based_on_id)
                if base is None or base["project_id"] != project_key:
                    raise conflict(
                        "invalid_base_version",
                        "Base script version must belong to this project",
                    )
            existing_row = conn.execute(
                "SELECT * FROM script_versions WHERE id = ? AND owner_user_id = ?",
                (script_key, self.owner_user_id),
            ).fetchone()
            if existing_row:
                existing = _payload(existing_row)
                if (
                    existing["project_id"] != project_key
                    or existing["version_number"] != version_number
                    or self._script_content(existing) != content
                ):
                    raise conflict(
                        "generated_script_identity_conflict",
                        "Generated script ID was already stored with different content or version",
                    )
                if requested_status != existing["review_status"]:
                    if requested_status == "pending" and existing["review_status"] != "pending":
                        return existing
                    decision = "approve" if requested_status == "approved" else "reject"
                    review = conn.execute(
                        "SELECT 1 FROM reviews WHERE owner_user_id = ? AND script_version_id = ? "
                        "AND decision = ? LIMIT 1",
                        (self.owner_user_id, script_key, decision),
                    ).fetchone()
                    if review is None:
                        raise conflict(
                            "script_review_not_persisted",
                            "Script review status can only follow a persisted human review",
                        )
                    existing["review_status"] = requested_status
                    conn.execute(
                        "UPDATE script_versions SET review_status = ?, payload = ? "
                        "WHERE id = ? AND owner_user_id = ?",
                        (requested_status, _json(existing), script_key, self.owner_user_id),
                    )
                return existing

            if requested_status != "pending":
                raise conflict(
                    "script_review_not_persisted",
                    "New generated scripts must begin in pending review status",
                )
            version_in_use = conn.execute(
                "SELECT id FROM script_versions WHERE project_id = ? AND version_number = ?",
                (project_key, version_number),
            ).fetchone()
            if version_in_use:
                raise conflict(
                    "script_version_number_conflict",
                    "Generated script version_number is already in use",
                    version_number=version_number,
                )
            result = {
                **content,
                "generation_result_id": generated.get("generation_result_id"),
                "fact_revision_refs": self._fact_revision_refs(book, content),
                "id": script_key,
                "project_id": project_key,
                "owner_user_id": self.owner_user_id,
                "version_number": version_number,
                "review_status": "pending",
                "created_at": created_at,
            }
            conn.execute(
                "INSERT INTO script_versions"
                "(id, owner_user_id, project_id, version_number, review_status, created_at, payload) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (
                    script_key,
                    self.owner_user_id,
                    project_key,
                    version_number,
                    created_at,
                    _json(result),
                ),
            )
            return result

    def create_run(
        self,
        project_id: UUID | str,
        data: Any,
        idempotency_key: str,
        *,
        submitter: SubmissionHook | None = None,
    ) -> JsonObject:
        project_key = _as_uuid(project_id)
        request_data = _dump(data)
        self._validate_idempotency_key(idempotency_key)
        hash_value = _request_hash({"project_id": project_key, **request_data})
        created = False
        with self.database.transaction() as conn:
            if self._get(conn, "creative_projects", project_key) is None:
                raise not_found("Creative project")
            existing = conn.execute(
                "SELECT * FROM runs WHERE owner_user_id = ? AND project_id = ? "
                "AND idempotency_key = ?",
                (self.owner_user_id, project_key, idempotency_key),
            ).fetchone()
            if existing:
                if existing["request_hash"] != hash_value:
                    raise conflict(
                        "idempotency_key_conflict",
                        "Idempotency-Key was already used with a different request",
                    )
                return _payload(existing)
            run_id = str(uuid4())
            now = _utc_now()
            result = {
                "id": run_id,
                "project_id": project_key,
                "owner_user_id": self.owner_user_id,
                "status": RunStatus.QUEUED.value,
                "stage": RunStage.PREPARE.value,
                "sequence": 1,
                "attempt": 1,
                "cancel_requested_at": None,
                "error_code": None,
                "error_message": None,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                "INSERT INTO runs"
                "(id, owner_user_id, project_id, brief_version, status, stage, sequence, "
                "attempt, cancel_requested_at, "
                "idempotency_key, request_hash, created_at, updated_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    self.owner_user_id,
                    project_key,
                    request_data["brief_version"],
                    result["status"],
                    result["stage"],
                    result["sequence"],
                    result["attempt"],
                    result["cancel_requested_at"],
                    idempotency_key,
                    hash_value,
                    now,
                    now,
                    _json(result),
                ),
            )
            event = self._run_event(
                run=result,
                event_type=RunEventType.CREATED.value,
                occurred_at=now,
                sequence=1,
                message="Run queued",
            )
            self._insert_event(conn, event)
            created = True
        if created and submitter is not None:
            try:
                submitter(dict(result))
            except Exception as exc:
                self.transition_run(
                    run_id,
                    status=RunStatus.FAILED.value,
                    event_type=RunEventType.FAILED.value,
                    reason_code="submission_failed",
                    message="Run could not be submitted to the worker",
                )
                raise DomainError(
                    503,
                    "submission_failed",
                    "Run could not be submitted to the worker",
                ) from exc
        return result

    def get_run(self, run_id: UUID | str) -> JsonObject:
        return self._get_or_404("runs", run_id, "Run")

    def get_generation_invocation(self, run_id: UUID | str, invocation_key: str) -> JsonObject | None:
        """Read a completed invocation and its private recovery script before any provider call."""
        run_key = _as_uuid(run_id)
        with self.database.read() as conn:
            if self._get(conn, "runs", run_key) is None:
                raise not_found("Run")
            row = conn.execute(
                "SELECT state, input_digest, recovery_script FROM generation_invocations "
                "WHERE owner_user_id = ? AND run_id = ? AND invocation_key = ?",
                (self.owner_user_id, run_key, invocation_key),
            ).fetchone()
            result_row = conn.execute(
                "SELECT payload FROM generation_results WHERE owner_user_id = ? AND run_id = ? AND invocation_key = ?",
                (self.owner_user_id, run_key, invocation_key),
            ).fetchone()
            if row is None and result_row is None:
                return None
            result = _payload(result_row) if result_row is not None else None
            cost = None
            if result is not None:
                cost_row = conn.execute(
                    "SELECT payload FROM model_cost_records WHERE owner_user_id = ? AND generation_result_id = ?",
                    (self.owner_user_id, result["id"]),
                ).fetchone()
                cost = _payload(cost_row) if cost_row is not None else None
            return {
                "state": row["state"] if row is not None else "completed",
                "input_digest": row["input_digest"] if row is not None else result["input_digest"],
                "recovery_script": json.loads(row["recovery_script"]) if row is not None and row["recovery_script"] else None,
                "generation_result": result,
                "cost_record": cost,
            }

    def reserve_generation_invocation(self, run_id: UUID | str, invocation_key: str, input_digest: str) -> bool:
        """Durably mark an external call before it can leave this process."""
        run_key = _as_uuid(run_id)
        with self.database.transaction() as conn:
            if self._get(conn, "runs", run_key) is None:
                raise not_found("Run")
            row = conn.execute(
                "SELECT run_id, input_digest FROM generation_invocations WHERE owner_user_id = ? AND invocation_key = ?",
                (self.owner_user_id, invocation_key),
            ).fetchone()
            if row is not None:
                if row["run_id"] != run_key or row["input_digest"] != input_digest:
                    raise conflict("idempotency_key_conflict", "Generation invocation key was reused with different input")
                return False
            conn.execute(
                "INSERT INTO generation_invocations(owner_user_id, run_id, invocation_key, input_digest, state) "
                "VALUES (?, ?, ?, ?, 'started')",
                (self.owner_user_id, run_key, invocation_key, input_digest),
            )
        return True

    def persist_generation(self, result_payload: Any, cost_payload: Any, recovery_script: Any = None) -> JsonObject:
        """Atomically record one invocation and one actual or unknown cost."""
        request = _dump(result_payload)
        cost_request = _dump(cost_payload)
        run_key = _as_uuid(request["run_id"])
        invocation_key = str(request.get("invocation_key") or "").strip()
        if not invocation_key:
            raise DomainError(422, "validation_error", "Generation invocation_key is required")
        source = request.get("source")
        status = request.get("status")
        reason = request.get("reason_code")
        if source not in {"deterministic_fixture", "external_model"} or status not in {"succeeded", "failed"}:
            raise DomainError(422, "validation_error", "Generation source or status is invalid")
        if status == "succeeded" and (reason is not None or not request.get("output_digest") or not request.get("script_version_id")):
            raise DomainError(422, "validation_error", "Successful generation requires a script ID and output digest")
        if status == "failed" and (reason not in {"provider_unavailable", "provider_timeout", "invalid_output", "unsupported_claim", "missing_fact_basis", "prohibited_claim", "cancelled", "manual_reconciliation_required"} or request.get("output_digest") or request.get("script_version_id")):
            raise DomainError(422, "validation_error", "Failed generation needs a stable reason and no script")
        for digest in (request.get("input_digest"), request.get("output_digest")):
            if digest is not None and not re.fullmatch(r"[a-f0-9]{64}", str(digest)):
                raise DomainError(422, "validation_error", "Generation digest must be SHA-256")
        amount = cost_request.get("amount_minor")
        missing = cost_request.get("missing_reason")
        if amount is None and not missing:
            raise DomainError(422, "validation_error", "Unknown cost requires missing_reason")
        if amount is not None and (not isinstance(amount, int) or amount < 0 or missing is not None):
            raise DomainError(422, "validation_error", "Actual cost must be nonnegative and have no missing reason")
        if source == "deterministic_fixture" and amount != 0:
            raise DomainError(422, "validation_error", "Deterministic generation has zero cost")
        with self.database.transaction() as conn:
            run = self._get(conn, "runs", run_key)
            if run is None:
                raise not_found("Run")
            invocation = conn.execute(
                "SELECT run_id, input_digest, recovery_script FROM generation_invocations WHERE owner_user_id = ? AND invocation_key = ?",
                (self.owner_user_id, invocation_key),
            ).fetchone()
            if invocation is not None and (invocation["run_id"] != run_key or invocation["input_digest"] != request["input_digest"]):
                raise conflict("idempotency_key_conflict", "Generation invocation key was reused with different input")
            recovery_json = _json(recovery_script) if recovery_script is not None else None
            if status == "succeeded" and source == "external_model" and recovery_json is None and invocation is not None and invocation["recovery_script"] is None:
                raise DomainError(422, "validation_error", "External generation requires a recoverable script")
            existing_row = conn.execute(
                "SELECT payload FROM generation_results WHERE owner_user_id = ? AND invocation_key = ?",
                (self.owner_user_id, invocation_key),
            ).fetchone()
            if existing_row is not None:
                existing = _payload(existing_row)
                if existing["run_id"] != run_key or any(
                    existing.get(key) != request.get(key)
                    for key in ("source", "provider_key", "model_key", "status", "reason_code",
                                "input_digest", "output_digest", "script_version_id")
                ):
                    raise conflict("idempotency_key_conflict", "Generation invocation key was reused with different content")
                cost_row = conn.execute(
                    "SELECT payload FROM model_cost_records WHERE owner_user_id = ? AND generation_result_id = ?",
                    (self.owner_user_id, existing["id"]),
                ).fetchone()
                existing_cost = _payload(cost_row)
                if any(existing_cost.get(key) != cost_request.get(key) for key in
                       ("currency", "amount_minor", "missing_reason", "input_tokens", "output_tokens")):
                    raise conflict("idempotency_key_conflict", "Generation cost changed on replay")
                if recovery_json is not None and invocation is not None and invocation["recovery_script"] != recovery_json:
                    raise conflict("idempotency_key_conflict", "Generation recovery script changed on replay")
                return {"generation_result": existing, "cost_record": existing_cost}
            result_id, cost_id, now = str(uuid4()), str(uuid4()), _utc_now()
            result = {
                "id": result_id, "run_id": run_key, "owner_user_id": self.owner_user_id,
                "attempt": int(run["attempt"]), "invocation_key": invocation_key,
                "source": source, "provider_key": str(request["provider_key"]),
                "model_key": str(request["model_key"]), "status": status,
                "reason_code": reason, "input_digest": request["input_digest"],
                "output_digest": request.get("output_digest"),
                "script_version_id": str(request["script_version_id"]) if request.get("script_version_id") else None,
                "created_at": now,
            }
            currency = cost_request.get("currency", "CNY")
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise DomainError(422, "validation_error", "Currency must be ISO 4217")
            cost = {
                "id": cost_id, "run_id": run_key, "owner_user_id": self.owner_user_id,
                "generation_result_id": result_id, "currency": currency,
                "amount_minor": amount, "missing_reason": missing,
                "input_tokens": cost_request.get("input_tokens"),
                "output_tokens": cost_request.get("output_tokens"), "recorded_at": now,
            }
            conn.execute(
                "INSERT INTO generation_results(id, owner_user_id, run_id, invocation_key, status, script_version_id, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (result_id, self.owner_user_id, run_key, invocation_key, status, result["script_version_id"], now, _json(result)),
            )
            conn.execute(
                "INSERT INTO model_cost_records(id, owner_user_id, run_id, generation_result_id, recorded_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cost_id, self.owner_user_id, run_key, result_id, now, _json(cost)),
            )
            if invocation is not None:
                conn.execute(
                    "UPDATE generation_invocations SET state = ?, recovery_script = ? "
                    "WHERE owner_user_id = ? AND invocation_key = ?",
                    ("manual_reconciliation_required" if reason == "manual_reconciliation_required" else "completed",
                     recovery_json, self.owner_user_id, invocation_key),
                )
        return {"generation_result": result, "cost_record": cost}

    def list_generation_results(self, run_id: UUID | str) -> list[JsonObject]:
        run_key = _as_uuid(run_id)
        with self.database.read() as conn:
            if self._get(conn, "runs", run_key) is None:
                raise not_found("Run")
            rows = conn.execute("SELECT payload FROM generation_results WHERE owner_user_id = ? AND run_id = ? ORDER BY created_at, id", (self.owner_user_id, run_key)).fetchall()
        return [_payload(row) for row in rows]

    def list_run_costs(self, run_id: UUID | str) -> list[JsonObject]:
        run_key = _as_uuid(run_id)
        with self.database.read() as conn:
            if self._get(conn, "runs", run_key) is None:
                raise not_found("Run")
            rows = conn.execute("SELECT payload FROM model_cost_records WHERE owner_user_id = ? AND run_id = ? ORDER BY recorded_at, id", (self.owner_user_id, run_key)).fetchall()
        return [_payload(row) for row in rows]

    def cancel_run(
        self, run_id: UUID | str, data: Any, idempotency_key: str
    ) -> JsonObject:
        run_key = _as_uuid(run_id)
        request_data = _dump(data)
        self._validate_idempotency_key(idempotency_key)
        reason = request_data.get("reason")
        operation = "cancel_run"
        request_hash = _request_hash(
            {"operation": operation, "run_id": run_key, "reason": reason}
        )
        with self.database.transaction() as conn:
            replay = self._get_idempotent_mutation(
                conn, idempotency_key, operation, run_key, request_hash
            )
            if replay is not None:
                return replay
            row = self._get_row(conn, "runs", run_key)
            if row is None:
                raise not_found("Run")
            current = _payload(row)
            status = current["status"]
            result = current
            if status == RunStatus.RUNNING.value:
                if current.get("cancel_requested_at") is None:
                    now = _utc_now()
                    result = {
                        **current,
                        "cancel_requested_at": now,
                        "sequence": current["sequence"] + 1,
                        "updated_at": now,
                    }
                    conn.execute(
                        "UPDATE runs SET sequence = ?, cancel_requested_at = ?, "
                        "updated_at = ?, payload = ? WHERE id = ? AND owner_user_id = ?",
                        (
                            result["sequence"],
                            now,
                            now,
                            _json(result),
                            run_key,
                            self.owner_user_id,
                        ),
                    )
                    self._insert_event(
                        conn,
                        self._run_event(
                            run=result,
                            event_type=RunEventType.CANCEL_REQUESTED.value,
                            occurred_at=now,
                            sequence=result["sequence"],
                            reason_code="creator_requested",
                            message=reason,
                            attempt=result["attempt"],
                        ),
                    )
            elif status in {RunStatus.QUEUED.value, RunStatus.WAITING_REVIEW.value}:
                now = _utc_now()
                result = {
                    **current,
                    "status": RunStatus.CANCELLED.value,
                    "error_code": None,
                    "error_message": None,
                    "sequence": current["sequence"] + 1,
                    "updated_at": now,
                }
                conn.execute(
                    "UPDATE runs SET status = ?, sequence = ?, updated_at = ?, "
                    "execution_claimed_at = NULL, review_resume_claimed_at = NULL, payload = ? "
                    "WHERE id = ? AND owner_user_id = ?",
                    (
                        result["status"],
                        result["sequence"],
                        now,
                        _json(result),
                        run_key,
                        self.owner_user_id,
                    ),
                )
                self._insert_event(
                    conn,
                    self._run_event(
                        run=result,
                        event_type=RunEventType.CANCELLED.value,
                        occurred_at=now,
                        sequence=result["sequence"],
                        reason_code="creator_cancelled",
                        message=reason,
                    ),
                )
            elif status != RunStatus.CANCELLED.value:
                raise conflict(
                    "invalid_state_transition",
                    "Only queued, running, or review-waiting runs can be cancelled",
                    current_status=status,
                )
            self._store_idempotent_mutation(
                conn, idempotency_key, operation, run_key, request_hash, result
            )
        return result

    def retry_run(self, run_id: UUID | str, idempotency_key: str) -> JsonObject:
        run_key = _as_uuid(run_id)
        self._validate_idempotency_key(idempotency_key)
        operation = "retry_run"
        request_hash = _request_hash({"operation": operation, "run_id": run_key})
        with self.database.transaction() as conn:
            replay = self._get_idempotent_mutation(
                conn, idempotency_key, operation, run_key, request_hash
            )
            if replay is not None:
                return replay
            row = self._get_row(conn, "runs", run_key)
            if row is None:
                raise not_found("Run")
            current = _payload(row)
            if current["status"] != RunStatus.FAILED.value:
                raise conflict(
                    "invalid_state_transition",
                    "Only a failed run can be retried",
                    current_status=current["status"],
                )
            now = _utc_now()
            next_attempt = current["attempt"] + 1
            result = {
                **current,
                "status": RunStatus.QUEUED.value,
                "stage": RunStage.PREPARE.value,
                "sequence": current["sequence"] + 1,
                "attempt": next_attempt,
                "cancel_requested_at": None,
                "error_code": None,
                "error_message": None,
                "updated_at": now,
            }
            conn.execute(
                "UPDATE runs SET status = ?, stage = ?, sequence = ?, attempt = ?, "
                "cancel_requested_at = NULL, execution_claimed_at = NULL, updated_at = ?, "
                "review_resume_claimed_at = NULL, "
                "payload = ? WHERE id = ? AND owner_user_id = ? AND status = 'failed'",
                (
                    result["status"],
                    result["stage"],
                    result["sequence"],
                    next_attempt,
                    now,
                    _json(result),
                    run_key,
                    self.owner_user_id,
                ),
            )
            self._insert_event(
                conn,
                self._run_event(
                    run=result,
                    event_type=RunEventType.RETRY_REQUESTED.value,
                    occurred_at=now,
                    sequence=result["sequence"],
                    reason_code="creator_retry_requested",
                    message=f"Retry queued for attempt {next_attempt}",
                    attempt=next_attempt,
                ),
            )
            self._store_idempotent_mutation(
                conn, idempotency_key, operation, run_key, request_hash, result
            )
        return result

    def is_run_cancel_requested(self, run_id: UUID | str) -> bool:
        return self.get_run(run_id).get("cancel_requested_at") is not None

    def confirm_run_cancelled(
        self, run_id: UUID | str, message: str | None = None
    ) -> JsonObject:
        run_key = _as_uuid(run_id)
        with self.database.transaction() as conn:
            row = self._get_row(conn, "runs", run_key)
            if row is None:
                raise not_found("Run")
            current = _payload(row)
            if current["status"] == RunStatus.CANCELLED.value:
                return current
            if (
                current["status"] != RunStatus.RUNNING.value
                or current.get("cancel_requested_at") is None
            ):
                raise conflict(
                    "invalid_state_transition",
                    "Only a running run with a cancellation request can confirm cancellation",
                )
            now = _utc_now()
            result = {
                **current,
                "status": RunStatus.CANCELLED.value,
                "sequence": current["sequence"] + 1,
                "error_code": None,
                "error_message": None,
                "updated_at": now,
            }
            conn.execute(
                "UPDATE runs SET status = ?, sequence = ?, updated_at = ?, "
                "execution_claimed_at = NULL, review_resume_claimed_at = NULL, payload = ? "
                "WHERE id = ? AND owner_user_id = ?",
                (
                    result["status"],
                    result["sequence"],
                    now,
                    _json(result),
                    run_key,
                    self.owner_user_id,
                ),
            )
            self._insert_event(
                conn,
                self._run_event(
                    run=result,
                    event_type=RunEventType.CANCELLED.value,
                    occurred_at=now,
                    sequence=result["sequence"],
                    reason_code="worker_cancelled",
                    message=message,
                ),
            )
        return result

    def get_run_snapshot(self, run_id: UUID | str) -> JsonObject:
        """Return a queued/active run with its stored brief version and inputs."""

        run_key = _as_uuid(run_id)
        with self.database.read() as conn:
            row = self._get_row(conn, "runs", run_key)
            if row is None:
                raise not_found("Run")
            run = _payload(row)
            brief_version = row["brief_version"]
        snapshot = self.get_generation_snapshot(run["project_id"])
        return {"run": run, "brief_version": brief_version, "snapshot": snapshot}

    def list_queued_runs(self, *, limit: int = 100) -> list[JsonObject]:
        """List this service owner's queued runs for worker polling."""

        self._validate_queue_limit(limit)
        with self.database.read() as conn:
            rows = conn.execute(
                "SELECT payload FROM runs WHERE owner_user_id = ? AND status = 'queued' "
                "ORDER BY created_at, id LIMIT ?",
                (self.owner_user_id, limit),
            ).fetchall()
        return [_payload(row) for row in rows]

    def claim_next_run(self) -> JsonObject | None:
        """Atomically claim one queued run and persist its running event."""

        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE owner_user_id = ? AND status = 'queued' "
                "ORDER BY created_at, id LIMIT 1",
                (self.owner_user_id,),
            ).fetchone()
            if row is None:
                return None
            current = _payload(row)
            sequence = current["sequence"] + 1
            now = _utc_now()
            updated = {
                **current,
                "status": RunStatus.RUNNING.value,
                "sequence": sequence,
                "error_code": None,
                "error_message": None,
                "updated_at": now,
            }
            conn.execute(
                "UPDATE runs SET status = ?, sequence = ?, execution_claimed_at = ?, "
                "updated_at = ?, payload = ? "
                "WHERE id = ? AND owner_user_id = ? AND status = 'queued'",
                (
                    updated["status"],
                    sequence,
                    now,
                    now,
                    _json(updated),
                    current["id"],
                    self.owner_user_id,
                ),
            )
            event = self._run_event(
                run=updated,
                event_type=RunEventType.STAGE_CHANGED.value,
                occurred_at=now,
                sequence=sequence,
                message="Run claimed by worker",
            )
            self._insert_event(conn, event)
        return updated

    def claim_next_resumable_run(
        self, *, lease_seconds: int = 300
    ) -> JsonObject | None:
        """Claim an expired non-review run lease so a new worker can resume its checkpoint."""

        self._validate_run_lease(lease_seconds)
        now_dt = datetime.now(timezone.utc)
        expired_before = (now_dt - timedelta(seconds=lease_seconds)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        claimed_at = _utc_now()
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE owner_user_id = ? AND status = 'running' "
                "AND stage <> 'review' "
                "AND (execution_claimed_at IS NULL OR execution_claimed_at <= ?) "
                "ORDER BY COALESCE(execution_claimed_at, created_at), created_at, id LIMIT 1",
                (self.owner_user_id, expired_before),
            ).fetchone()
            if row is None:
                return None
            run = _payload(row)
            updated = conn.execute(
                "UPDATE runs SET execution_claimed_at = ? WHERE id = ? "
                "AND owner_user_id = ? AND status = 'running' "
                "AND (execution_claimed_at IS NULL OR execution_claimed_at <= ?)",
                (claimed_at, run["id"], self.owner_user_id, expired_before),
            )
            if updated.rowcount != 1:
                return None
        return run

    def renew_run_execution_claim(self, run_id: UUID | str) -> bool:
        run_key = _as_uuid(run_id)
        claimed_at = _utc_now()
        with self.database.transaction() as conn:
            row = self._get_row(conn, "runs", run_key)
            if row is None:
                raise not_found("Run")
            run = _payload(row)
            if (
                run["status"] != RunStatus.RUNNING.value
                or run["stage"] == RunStage.REVIEW.value
                or run.get("cancel_requested_at") is not None
            ):
                return False
            cursor = conn.execute(
                "UPDATE runs SET execution_claimed_at = ? WHERE id = ? "
                "AND owner_user_id = ? AND status = 'running' "
                "AND stage <> 'review' AND cancel_requested_at IS NULL",
                (claimed_at, run_key, self.owner_user_id),
            )
            return cursor.rowcount == 1

    def release_run_execution_claim(self, run_id: UUID | str) -> None:
        run_key = _as_uuid(run_id)
        with self.database.transaction() as conn:
            row = self._get_row(conn, "runs", run_key)
            if row is None:
                raise not_found("Run")
            conn.execute(
                "UPDATE runs SET execution_claimed_at = NULL WHERE id = ? "
                "AND owner_user_id = ?",
                (run_key, self.owner_user_id),
            )

    def claim_next_reviewed_run(self, *, lease_seconds: int = 300) -> JsonObject | None:
        """Claim one human-reviewed run for LangGraph resume, with an expiring lease.

        A worker releases the claim by persisting the next run status/stage event. If
        it crashes first, another worker can reclaim the run after the lease expires.
        """

        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 3600
        ):
            raise DomainError(
                422, "invalid_lease", "Review resume lease must be between 1 and 3600 seconds"
            )
        now_dt = datetime.now(timezone.utc)
        expired_before = (now_dt - timedelta(seconds=lease_seconds)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        claimed_at = _utc_now()
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT r.* FROM runs r WHERE r.owner_user_id = ? AND r.status = 'running' "
                "AND r.stage = 'review' AND EXISTS ("
                "SELECT 1 FROM reviews v WHERE v.run_id = r.id AND v.owner_user_id = r.owner_user_id"
                ") AND (r.review_resume_claimed_at IS NULL OR r.review_resume_claimed_at <= ?) "
                "ORDER BY r.updated_at, r.id LIMIT 1",
                (self.owner_user_id, expired_before),
            ).fetchone()
            if row is None:
                return None
            run = _payload(row)
            conn.execute(
                "UPDATE runs SET review_resume_claimed_at = ? WHERE id = ? "
                "AND owner_user_id = ?",
                (claimed_at, run["id"], self.owner_user_id),
            )
        return run

    def get_review_request(self, run_id: UUID | str) -> JsonObject:
        """Read the latest persisted human review request for this owner's run."""

        run_key = _as_uuid(run_id)
        with self.database.read() as conn:
            if self._get(conn, "runs", run_key) is None:
                raise not_found("Run")
            row = conn.execute(
                "SELECT payload FROM reviews WHERE run_id = ? AND owner_user_id = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (run_key, self.owner_user_id),
            ).fetchone()
        if row is None:
            raise not_found("Review request")
        return _payload(row)["request"]

    def release_review_resume_claim(self, run_id: UUID | str) -> None:
        """Release a failed worker's resume lease so another poll can retry now."""

        run_key = _as_uuid(run_id)
        with self.database.transaction() as conn:
            row = self._get_row(conn, "runs", run_key)
            if row is None:
                raise not_found("Run")
            run = _payload(row)
            if run["status"] != RunStatus.RUNNING.value or run["stage"] != RunStage.REVIEW.value:
                raise conflict(
                    "run_not_claimed_for_review_resume",
                    "Only a running review-stage run can release its resume claim",
                )
            conn.execute(
                "UPDATE runs SET review_resume_claimed_at = NULL "
                "WHERE id = ? AND owner_user_id = ?",
                (run_key, self.owner_user_id),
            )

    def list_queued_renders(self, *, limit: int = 100) -> list[JsonObject]:
        """List this service owner's queued renders for worker polling."""

        self._validate_queue_limit(limit)
        with self.database.read() as conn:
            rows = conn.execute(
                "SELECT payload FROM render_jobs WHERE owner_user_id = ? AND status = 'queued' "
                "ORDER BY created_at, id LIMIT ?",
                (self.owner_user_id, limit),
            ).fetchall()
        return [_payload(row) for row in rows]

    def claim_next_render(self) -> JsonObject | None:
        """Atomically claim one queued render for a worker process."""

        with self.database.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM render_jobs WHERE owner_user_id = ? AND status = 'queued' "
                "ORDER BY created_at, id",
                (self.owner_user_id,),
            ).fetchall()
            for row in rows:
                current = _payload(row)
                candidate = self._get(
                    conn, "promotion_candidates", row["promotion_candidate_id"]
                )
                if candidate is None or candidate.get("eligibility_status") != MANUALLY_CONFIRMED:
                    now = _utc_now()
                    failed = {
                        **current,
                        "status": RunStatus.FAILED.value,
                        "output_ref": None,
                        "error_message": (
                            "Promotion eligibility must be manually confirmed before rendering"
                        ),
                        "updated_at": now,
                    }
                    conn.execute(
                        "UPDATE render_jobs SET status = ?, execution_claimed_at = NULL, "
                        "updated_at = ?, payload = ? "
                        "WHERE id = ? AND owner_user_id = ? AND status = 'queued'",
                        (
                            failed["status"],
                            now,
                            _json(failed),
                            current["id"],
                            self.owner_user_id,
                        ),
                    )
                    continue
                now = _utc_now()
                updated = {
                    **current,
                    "status": RunStatus.RUNNING.value,
                    "error_message": None,
                    "updated_at": now,
                }
                conn.execute(
                    "UPDATE render_jobs SET status = ?, execution_claimed_at = ?, "
                    "updated_at = ?, payload = ? "
                    "WHERE id = ? AND owner_user_id = ? AND status = 'queued'",
                    (
                        updated["status"],
                        now,
                        now,
                        _json(updated),
                        current["id"],
                        self.owner_user_id,
                    ),
                )
                return updated
        return None

    def claim_next_resumable_render(
        self, *, lease_seconds: int = 300
    ) -> JsonObject | None:
        """Claim an expired running render so a worker can safely resume or confirm cancel."""

        self._validate_run_lease(lease_seconds)
        expired_before = (
            datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        claimed_at = _utc_now()
        with self.database.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM render_jobs WHERE owner_user_id = ? AND status = 'running' "
                "AND (execution_claimed_at IS NULL OR execution_claimed_at <= ?) "
                "ORDER BY COALESCE(execution_claimed_at, created_at), created_at, id",
                (self.owner_user_id, expired_before),
            ).fetchall()
            for row in rows:
                current = _payload(row)
                candidate = self._get(
                    conn, "promotion_candidates", row["promotion_candidate_id"]
                )
                cancel_requested = current.get("cancel_requested_at") is not None
                if (
                    not cancel_requested
                    and (
                        candidate is None
                        or candidate.get("eligibility_status") != MANUALLY_CONFIRMED
                    )
                ):
                    now = _utc_now()
                    failed = {
                        **current,
                        "status": RunStatus.FAILED.value,
                        "output_ref": None,
                        "error_message": (
                            "Promotion eligibility must be manually confirmed before rendering"
                        ),
                        "updated_at": now,
                    }
                    conn.execute(
                        "UPDATE render_jobs SET status = ?, execution_claimed_at = NULL, "
                        "updated_at = ?, payload = ? WHERE id = ? AND owner_user_id = ?",
                        (
                            failed["status"],
                            now,
                            _json(failed),
                            current["id"],
                            self.owner_user_id,
                        ),
                    )
                    continue
                claimed = conn.execute(
                    "UPDATE render_jobs SET execution_claimed_at = ? WHERE id = ? "
                    "AND owner_user_id = ? AND status = 'running' "
                    "AND (execution_claimed_at IS NULL OR execution_claimed_at <= ?)",
                    (claimed_at, current["id"], self.owner_user_id, expired_before),
                )
                if claimed.rowcount == 1:
                    return current
        return None

    def renew_render_execution_claim(self, render_id: UUID | str) -> bool:
        render_key = _as_uuid(render_id)
        claimed_at = _utc_now()
        with self.database.transaction() as conn:
            row = self._get_row(conn, "render_jobs", render_key)
            if row is None:
                raise not_found("Render job")
            render = _payload(row)
            if (
                render["status"] != RunStatus.RUNNING.value
                or render.get("cancel_requested_at") is not None
            ):
                return False
            cursor = conn.execute(
                "UPDATE render_jobs SET execution_claimed_at = ? WHERE id = ? "
                "AND owner_user_id = ? AND status = 'running' "
                "AND cancel_requested_at IS NULL",
                (claimed_at, render_key, self.owner_user_id),
            )
            return cursor.rowcount == 1

    def release_render_execution_claim(self, render_id: UUID | str) -> None:
        render_key = _as_uuid(render_id)
        with self.database.transaction() as conn:
            row = self._get_row(conn, "render_jobs", render_key)
            if row is None:
                raise not_found("Render job")
            conn.execute(
                "UPDATE render_jobs SET execution_claimed_at = NULL WHERE id = ? "
                "AND owner_user_id = ?",
                (render_key, self.owner_user_id),
            )

    def get_generation_snapshot(self, project_id: UUID | str) -> JsonObject:
        """Return the owner-scoped, manual-input snapshot consumed by script generation."""

        project_key = _as_uuid(project_id)
        with self.database.read() as conn:
            project = self._get(conn, "creative_projects", project_key)
            if project is None:
                raise not_found("Creative project")
            book = self._get(conn, "books", project["book_id"])
            candidate = self._get(
                conn, "promotion_candidates", project["promotion_candidate_id"]
            )
            if book is None or candidate is None:
                raise conflict("project_references_missing", "Project references are unavailable")
            evidence_ids = list(project.get("evidence_ids", []))
            evidence_by_id: dict[str, JsonObject] = {}
            for fact in book.get("facts", []):
                if fact.get("evidence_id"):
                    evidence_ids.append(fact["evidence_id"])
            for evidence_id in dict.fromkeys(evidence_ids):
                evidence = self._get(conn, "evidence", evidence_id)
                if evidence is not None:
                    evidence_by_id[evidence_id] = evidence
            script_rows = conn.execute(
                "SELECT payload FROM script_versions WHERE project_id = ? "
                "AND owner_user_id = ? ORDER BY version_number",
                (project_key, self.owner_user_id),
            ).fetchall()
            scripts = [_payload(row) for row in script_rows]
        style_guidance_hints = self.list_approved_style_guidance(book["id"])
        return {
            "owner_user_id": self.owner_user_id,
            "project": project,
            "book": book,
            "promotion_candidate": candidate,
            "evidence": list(evidence_by_id.values()),
            "script_versions": scripts,
            "style_guidance": style_guidance_hints,
            "asset_rights": [
                {"origin": "self_created_text", "usage_status": "allowed"},
                {"origin": "self_created_graphic", "usage_status": "allowed"},
            ],
        }

    def get_script_version(self, script_version_id: UUID | str) -> JsonObject:
        return self._get_or_404("script_versions", script_version_id, "Script version")

    def get_render_snapshot(self, render_id: UUID | str) -> JsonObject:
        """Return render inputs and rights for the owner-scoped render worker."""

        render_key = _as_uuid(render_id)
        with self.database.read() as conn:
            row = self._get_row(conn, "render_jobs", render_key)
            if row is None:
                raise not_found("Render job")
            render = _payload(row)
            project = self._get(conn, "creative_projects", render["project_id"])
            script = self._get(conn, "script_versions", render["script_version_id"])
            candidate_id = row["promotion_candidate_id"]
            candidate = self._get(conn, "promotion_candidates", candidate_id)
            book = self._get(conn, "books", project["book_id"]) if project else None
            if not project or not script or not candidate or not book:
                raise conflict("render_references_missing", "Render references are unavailable")
            self._validate_script_fact_revisions(book, script)
            rights = json.loads(row["asset_rights"])
            audio_asset_id = row["audio_asset_id"]
            audio_asset = self._get(conn, "audio_assets", audio_asset_id) if audio_asset_id else None
            if audio_asset_id and audio_asset is None:
                raise not_found("Audio asset")
            if audio_asset is not None:
                audio_asset = self._current_audio_status(audio_asset)
                if audio_asset["usage_status"] != "allowed":
                    raise DomainError(422, "asset_rights_blocked", "Audio rights are not current")
        return {
            "owner_user_id": self.owner_user_id,
            "render": render,
            "idempotency_key": row["idempotency_key"],
            "project": project,
            "book": book,
            "promotion_candidate": candidate,
            "script_version": script,
            "asset_rights": rights,
            "audio_asset_id": audio_asset_id,
            "audio_asset": audio_asset,
        }

    def list_run_events(
        self, run_id: UUID | str, *, after_sequence: int = 0
    ) -> list[JsonObject]:
        run_key = _as_uuid(run_id)
        if after_sequence < 0:
            raise DomainError(422, "invalid_sequence", "Last-Event-ID must be non-negative")
        with self.database.read() as conn:
            if self._get(conn, "runs", run_key) is None:
                raise not_found("Run")
            rows = conn.execute(
                "SELECT payload FROM run_events WHERE run_id = ? AND owner_user_id = ? "
                "AND sequence > ? ORDER BY sequence",
                (run_key, self.owner_user_id, after_sequence),
            ).fetchall()
        return [_payload(row) for row in rows]

    def transition_run(
        self,
        run_id: UUID | str,
        *,
        status: str | None = None,
        stage: str | None = None,
        event_type: str | None = None,
        script_version_id: UUID | str | None = None,
        render_id: UUID | str | None = None,
        reason_code: str | None = None,
        message: str | None = None,
    ) -> JsonObject:
        run_key = _as_uuid(run_id)
        script_key = _as_uuid(script_version_id) if script_version_id else None
        render_key = _as_uuid(render_id) if render_id else None
        with self.database.transaction() as conn:
            row = self._get_row(conn, "runs", run_key)
            if row is None:
                raise not_found("Run")
            current = _payload(row)
            target_status = status or current["status"]
            target_stage = stage or current["stage"]
            if current.get("cancel_requested_at") and target_status != RunStatus.CANCELLED.value:
                raise conflict(
                    "invalid_state_transition",
                    "A cancellation-requested run can only be confirmed as cancelled",
                )
            if (
                target_status == RunStatus.CANCELLED.value
                and current["status"] == RunStatus.RUNNING.value
                and current.get("cancel_requested_at") is None
            ):
                raise conflict(
                    "invalid_state_transition",
                    "A running run must have a cancellation request before cancellation",
                )
            validate_run_transition(current["status"], target_status)
            try:
                target_stage = RunStage(target_stage).value
            except ValueError as exc:
                raise DomainError(422, "invalid_run_stage", "Unknown run stage") from exc
            if target_status == current["status"] and target_stage == current["stage"]:
                raise conflict(
                    "run_unchanged", "A run update must change its status or stage"
                )
            resolved_event_type = event_type or self._event_type_for_status(target_status)
            self._validate_event_type(
                resolved_event_type, target_status, current.get("attempt")
            )
            if resolved_event_type in {
                RunEventType.CREATED.value,
                RunEventType.REVIEWED.value,
            }:
                raise DomainError(
                    422,
                    "invalid_event_type",
                    "This event type is reserved for the corresponding domain operation",
                )
            sequence = current["sequence"] + 1
            now = _utc_now()
            updated = {
                **current,
                "status": target_status,
                "stage": target_stage,
                "sequence": sequence,
                "error_code": reason_code if target_status == "failed" else None,
                "error_message": message if target_status == "failed" else None,
                "updated_at": now,
            }
            execution_claimed_at = (
                now
                if target_status == RunStatus.RUNNING.value
                and target_stage != RunStage.REVIEW.value
                else None
            )
            conn.execute(
                "UPDATE runs SET status = ?, stage = ?, sequence = ?, attempt = ?, "
                "cancel_requested_at = ?, execution_claimed_at = ?, updated_at = ?, "
                "review_resume_claimed_at = NULL, payload = ? "
                "WHERE id = ? AND owner_user_id = ?",
                (
                    target_status,
                    target_stage,
                    sequence,
                    updated["attempt"],
                    updated.get("cancel_requested_at"),
                    execution_claimed_at,
                    now,
                    _json(updated),
                    run_key,
                    self.owner_user_id,
                ),
            )
            event = self._run_event(
                run=updated,
                event_type=resolved_event_type,
                occurred_at=now,
                sequence=sequence,
                script_version_id=script_key,
                render_id=render_key,
                reason_code=reason_code,
                message=message,
            )
            self._insert_event(conn, event)
        return updated

    def persist_run_event(self, data: Any) -> JsonObject:
        """Persist a worker RunEvent while preserving its event ID and sequence.

        Replays of an identical `(run_id, sequence)` event are idempotent. New events
        must append exactly the next sequence and cannot impersonate API-owned review
        decisions or run creation events.
        """

        raw = _dump(data)
        required = {
            "event_id",
            "run_id",
            "project_id",
            "sequence",
            "type",
            "status",
            "stage",
            "occurred_at",
        }
        if not required.issubset(raw):
            raise DomainError(
                422,
                "invalid_run_event",
                "RunEvent is missing required contract fields",
                details={"missing_fields": sorted(required - set(raw))},
            )
        event_id = _as_uuid(raw["event_id"])
        run_key = _as_uuid(raw["run_id"])
        project_key = _as_uuid(raw["project_id"])
        sequence = raw["sequence"]
        if not isinstance(sequence, int) or sequence < 1:
            raise DomainError(422, "invalid_sequence", "RunEvent sequence must be positive")
        try:
            event_type = RunEventType(raw["type"]).value
            event_status = RunStatus(raw["status"]).value
            event_stage = RunStage(raw["stage"]).value
        except ValueError as exc:
            raise DomainError(422, "invalid_run_event", "RunEvent contains an unknown enum") from exc
        occurred_at = _timestamp(raw["occurred_at"])
        if occurred_at > datetime.now(timezone.utc):
            raise DomainError(422, "invalid_run_event_time", "RunEvent cannot be from the future")
        script_key = _as_uuid(raw["script_version_id"]) if raw.get("script_version_id") else None
        render_key = _as_uuid(raw["render_id"]) if raw.get("render_id") else None
        event = {
            "event_id": event_id,
            "run_id": run_key,
            "project_id": project_key,
            "sequence": sequence,
            "type": event_type,
            "status": event_status,
            "stage": event_stage,
            "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
            "script_version_id": script_key,
            "render_id": render_key,
            "reason_code": raw.get("reason_code"),
            "message": raw.get("message"),
        }
        for field in ("generation_result_id", "cost_record_id"):
            if raw.get(field):
                event[field] = _as_uuid(raw[field])
        if "attempt" in raw:
            event["attempt"] = raw["attempt"]
        with self.database.transaction() as conn:
            by_event_id = conn.execute(
                "SELECT payload FROM run_events WHERE event_id = ? AND owner_user_id = ?",
                (event_id, self.owner_user_id),
            ).fetchone()
            by_sequence = conn.execute(
                "SELECT payload FROM run_events WHERE run_id = ? AND sequence = ? "
                "AND owner_user_id = ?",
                (run_key, sequence, self.owner_user_id),
            ).fetchone()
            if by_event_id or by_sequence:
                existing = _payload(by_event_id or by_sequence)
                same_identity = (
                    by_event_id is not None
                    and by_sequence is not None
                    and existing["event_id"] == event_id
                    and existing["run_id"] == run_key
                    and existing["sequence"] == sequence
                )
                same_content = {
                    key: value for key, value in existing.items() if key != "occurred_at"
                } == {key: value for key, value in event.items() if key != "occurred_at"}
                if same_identity and same_content:
                    return existing
                raise conflict(
                    "run_event_identity_conflict",
                    "RunEvent ID or sequence was already used with different content",
                )
            run_row = self._get_row(conn, "runs", run_key)
            if run_row is None:
                raise not_found("Run")
            run = _payload(run_row)
            if run["project_id"] != project_key:
                raise conflict("run_event_project_mismatch", "RunEvent project does not match run")
            if event_type in {
                RunEventType.CREATED.value,
                RunEventType.REVIEWED.value,
                RunEventType.CANCEL_REQUESTED.value,
                RunEventType.RETRY_REQUESTED.value,
            }:
                raise conflict(
                    "reserved_run_event",
                    "Run creation, review, cancellation, and retry events are persisted by domain operations",
                )
            if sequence != run["sequence"] + 1:
                raise conflict(
                    "run_event_sequence_gap",
                    "RunEvent sequence must immediately follow the current run sequence",
                    expected_sequence=run["sequence"] + 1,
                    actual_sequence=sequence,
                )
            if (
                event_status == RunStatus.CANCELLED.value
                and run["status"] == RunStatus.RUNNING.value
                and run.get("cancel_requested_at") is None
            ):
                raise conflict(
                    "invalid_state_transition",
                    "A running run must have a cancellation request before cancellation",
                )
            if run.get("cancel_requested_at") and event_status != RunStatus.CANCELLED.value:
                raise conflict(
                    "invalid_state_transition",
                    "A cancellation-requested run can only be confirmed as cancelled",
                )
            validate_run_transition(run["status"], event_status)
            self._validate_event_type(event_type, event_status, raw.get("attempt"))
            if (
                event_status == run["status"]
                and event_stage == run["stage"]
                and event_type not in {
                    RunEventType.REVIEW_REQUESTED.value,
                    RunEventType.GENERATION_COMPLETED.value,
                    RunEventType.GENERATION_FAILED.value,
                    RunEventType.COST_RECORDED.value,
                }
            ):
                raise conflict("run_unchanged", "RunEvent must change status or stage")
            if event_type in {
                RunEventType.GENERATION_COMPLETED.value,
                RunEventType.GENERATION_FAILED.value,
                RunEventType.COST_RECORDED.value,
            }:
                result_id = event.get("generation_result_id")
                result = self._get(conn, "generation_results", result_id) if result_id else None
                if result is None or result["run_id"] != run_key:
                    raise not_found("Generation result")
                if event_type == RunEventType.COST_RECORDED.value:
                    cost_id = event.get("cost_record_id")
                    cost = self._get(conn, "model_cost_records", cost_id) if cost_id else None
                    if cost is None or cost["generation_result_id"] != result_id:
                        raise not_found("Model cost record")
            if script_key:
                script = self._get(conn, "script_versions", script_key)
                if script is None or script["project_id"] != project_key:
                    raise not_found("Script version")
            if render_key:
                render = self._get(conn, "render_jobs", render_key)
                if render is None or render["project_id"] != project_key:
                    raise not_found("Render job")
            updated = {
                **run,
                "status": event_status,
                "stage": event_stage,
                "sequence": sequence,
                "error_code": event["reason_code"] if event_status == "failed" else None,
                "error_message": event["message"] if event_status == "failed" else None,
                "updated_at": event["occurred_at"],
            }
            execution_claimed_at = (
                event["occurred_at"]
                if event_status == RunStatus.RUNNING.value
                and event_stage != RunStage.REVIEW.value
                else None
            )
            conn.execute(
                "UPDATE runs SET status = ?, stage = ?, sequence = ?, attempt = ?, "
                "cancel_requested_at = ?, execution_claimed_at = ?, updated_at = ?, "
                "review_resume_claimed_at = NULL, payload = ? "
                "WHERE id = ? AND owner_user_id = ?",
                (
                    event_status,
                    event_stage,
                    sequence,
                    updated["attempt"],
                    updated.get("cancel_requested_at"),
                    execution_claimed_at,
                    event["occurred_at"],
                    _json(updated),
                    run_key,
                    self.owner_user_id,
                ),
            )
            self._insert_event(conn, event)
        return event

    def review_run(self, run_id: UUID | str, data: Any) -> JsonObject:
        run_key = _as_uuid(run_id)
        review_data = _dump(data)
        script_key = _as_uuid(review_data["script_version_id"])
        with self.database.transaction() as conn:
            run_row = self._get_row(conn, "runs", run_key)
            if run_row is None:
                raise not_found("Run")
            run = _payload(run_row)
            prior = conn.execute(
                "SELECT payload FROM reviews WHERE owner_user_id = ? AND run_id = ? "
                "AND script_version_id = ? ORDER BY created_at DESC LIMIT 1",
                (self.owner_user_id, run_key, script_key),
            ).fetchone()
            normalized_review = {
                **review_data,
                "script_version_id": script_key,
            }
            if prior:
                existing_review = _payload(prior)
                if existing_review["request"] != normalized_review:
                    raise conflict(
                        "review_already_submitted",
                        "A different review decision already exists for this script",
                    )
                return run
            if run["status"] != RunStatus.WAITING_REVIEW.value:
                raise conflict(
                    "run_not_waiting_review",
                    "Run must be waiting for review before a decision is submitted",
                )
            project = self._get(conn, "creative_projects", run["project_id"])
            script = self._get(conn, "script_versions", script_key)
            if script is None or script["project_id"] != run["project_id"]:
                raise not_found("Script version")
            if script["review_status"] != "pending":
                raise conflict(
                    "script_already_reviewed", "Only a pending script version can be reviewed"
                )

            if review_data["decision"] == "approve":
                self._validate_approval(conn, project, review_data)
                next_script_status = "approved"
            else:
                next_script_status = "rejected"
            # Keep the run in the review stage until a worker claims and resumes it.
            next_stage = RunStage.REVIEW.value

            review_id = str(uuid4())
            now = _utc_now()
            review_record = {
                "id": review_id,
                "run_id": run_key,
                "project_id": run["project_id"],
                "script_version_id": script_key,
                "owner_user_id": self.owner_user_id,
                "decision": review_data["decision"],
                "created_at": now,
                "request": normalized_review,
            }
            conn.execute(
                "INSERT INTO reviews"
                "(id, owner_user_id, run_id, script_version_id, decision, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    self.owner_user_id,
                    run_key,
                    script_key,
                    review_data["decision"],
                    now,
                    _json(review_record),
                ),
            )
            script.update(review_status=next_script_status)
            conn.execute(
                "UPDATE script_versions SET review_status = ?, payload = ? "
                "WHERE id = ? AND owner_user_id = ?",
                (next_script_status, _json(script), script_key, self.owner_user_id),
            )
            sequence = run["sequence"] + 1
            updated_run = {
                **run,
                "status": RunStatus.RUNNING.value,
                "stage": next_stage,
                "sequence": sequence,
                "error_code": None,
                "error_message": None,
                "updated_at": now,
            }
            conn.execute(
                "UPDATE runs SET status = ?, stage = ?, sequence = ?, updated_at = ?, "
                "execution_claimed_at = NULL, review_resume_claimed_at = NULL, payload = ? "
                "WHERE id = ? AND owner_user_id = ?",
                (
                    updated_run["status"],
                    next_stage,
                    sequence,
                    now,
                    _json(updated_run),
                    run_key,
                    self.owner_user_id,
                ),
            )
            event = self._run_event(
                run=updated_run,
                event_type=RunEventType.REVIEWED.value,
                occurred_at=now,
                sequence=sequence,
                script_version_id=script_key,
                message=f"Script review {review_data['decision']}",
            )
            self._insert_event(conn, event)
        return updated_run

    def create_render(
        self,
        data: Any,
        idempotency_key: str,
        *,
        submitter: SubmissionHook | None = None,
    ) -> JsonObject:
        render_data = _dump(data)
        self._validate_idempotency_key(idempotency_key)
        project_key = _as_uuid(render_data["project_id"])
        script_key = _as_uuid(render_data["script_version_id"])
        candidate_key = _as_uuid(render_data["promotion_candidate_id"])
        audio_asset_id = _as_uuid(render_data["audio_asset_id"]) if render_data.get("audio_asset_id") else None
        hash_value = _request_hash(render_data)
        if not render_data.get("asset_rights"):
            raise conflict(
                "asset_rights_missing",
                "A render requires at least one explicitly rights-cleared asset",
            )
        normalized_rights = [self._validate_asset_rights(item) for item in render_data["asset_rights"]]
        origins = {item["origin"] for item in normalized_rights}
        required_origins = {"self_created_text", "self_created_graphic"}
        if not required_origins.issubset(origins):
            raise conflict(
                "self_created_assets_required",
                "The Stage 1 render requires self-created text and graphic assets",
                missing_origins=sorted(required_origins - origins),
            )
        created = False
        with self.database.transaction() as conn:
            project = self._get(conn, "creative_projects", project_key)
            script = self._get(conn, "script_versions", script_key)
            candidate = self._get(conn, "promotion_candidates", candidate_key)
            if project is None:
                raise not_found("Creative project")
            if script is None or script["project_id"] != project_key:
                raise not_found("Script version")
            if candidate is None:
                raise not_found("Promotion candidate")
            if audio_asset_id is not None:
                audio_asset = self._get(conn, "audio_assets", audio_asset_id)
                if audio_asset is None:
                    raise not_found("Audio asset")
                if self._current_audio_status(audio_asset)["usage_status"] != "allowed":
                    raise DomainError(422, "asset_rights_blocked", "Audio rights are not current")
            if (
                candidate_key != project["promotion_candidate_id"]
                or candidate["book_id"] != project["book_id"]
            ):
                raise conflict(
                    "product_book_mismatch",
                    "Render promotion candidate must match the project edition and product",
                )
            if script["review_status"] != "approved":
                raise conflict(
                    "script_not_approved", "Only an approved script version can be rendered"
                )
            book = self._get(conn, "books", project["book_id"])
            self._validate_script_fact_revisions(book, script)
            if candidate["eligibility_status"] != MANUALLY_CONFIRMED:
                raise conflict(
                    "product_not_confirmed",
                    "Promotion candidate eligibility must be manually confirmed before rendering",
                )
            existing = conn.execute(
                "SELECT * FROM render_jobs WHERE owner_user_id = ? AND idempotency_key = ?",
                (self.owner_user_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["request_hash"] != hash_value:
                    raise conflict(
                        "idempotency_key_conflict",
                        "Idempotency-Key was already used with a different request",
                    )
                return _payload(existing)
            render_id = str(uuid4())
            now = _utc_now()
            result = {
                "id": render_id,
                "project_id": project_key,
                "script_version_id": script_key,
                "audio_asset_id": audio_asset_id,
                "owner_user_id": self.owner_user_id,
                "status": RunStatus.QUEUED.value,
                "attempt": 1,
                "cancel_requested_at": None,
                "output_ref": None,
                "error_message": None,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                "INSERT INTO render_jobs"
                "(id, owner_user_id, project_id, script_version_id, promotion_candidate_id, "
                "status, idempotency_key, request_hash, asset_rights, audio_asset_id, attempt, "
                "cancel_requested_at, created_at, updated_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    render_id,
                    self.owner_user_id,
                    project_key,
                    script_key,
                    candidate_key,
                    result["status"],
                    idempotency_key,
                    hash_value,
                    _json(normalized_rights),
                    audio_asset_id,
                    result["attempt"],
                    result["cancel_requested_at"],
                    now,
                    now,
                    _json(result),
                ),
            )
            created = True
        if created and submitter is not None:
            try:
                submitter(dict(result))
            except Exception as exc:
                self.transition_render(
                    render_id,
                    status=RunStatus.FAILED.value,
                    error_message="Render could not be submitted to the worker",
                )
                raise DomainError(
                    503,
                    "submission_failed",
                    "Render could not be submitted to the worker",
                ) from exc
        return result

    def get_render(self, render_id: UUID | str) -> JsonObject:
        return self._get_or_404("render_jobs", render_id, "Render job")

    def cancel_render(
        self, render_id: UUID | str, data: Any, idempotency_key: str
    ) -> JsonObject:
        render_key = _as_uuid(render_id)
        request_data = _dump(data)
        self._validate_idempotency_key(idempotency_key)
        reason = request_data.get("reason")
        operation = "cancel_render"
        request_hash = _request_hash(
            {"operation": operation, "render_id": render_key, "reason": reason}
        )
        with self.database.transaction() as conn:
            replay = self._get_idempotent_mutation(
                conn, idempotency_key, operation, render_key, request_hash
            )
            if replay is not None:
                return replay
            row = self._get_row(conn, "render_jobs", render_key)
            if row is None:
                raise not_found("Render job")
            current = _payload(row)
            status = current["status"]
            result = current
            if status == RunStatus.RUNNING.value:
                if current.get("cancel_requested_at") is None:
                    now = _utc_now()
                    result = {
                        **current,
                        "cancel_requested_at": now,
                        "updated_at": now,
                    }
                    conn.execute(
                        "UPDATE render_jobs SET cancel_requested_at = ?, cancel_reason = ?, updated_at = ?, "
                        "payload = ? WHERE id = ? AND owner_user_id = ?",
                        (now, reason, now, _json(result), render_key, self.owner_user_id),
                    )
            elif status == RunStatus.QUEUED.value:
                now = _utc_now()
                result = {
                    **current,
                    "status": RunStatus.CANCELLED.value,
                    "output_ref": None,
                    "error_message": None,
                    "updated_at": now,
                }
                conn.execute(
                    "UPDATE render_jobs SET status = ?, cancel_reason = ?, updated_at = ?, payload = ? "
                    "WHERE id = ? AND owner_user_id = ?",
                    (
                        result["status"],
                        reason,
                        now,
                        _json(result),
                        render_key,
                        self.owner_user_id,
                    ),
                )
            elif status != RunStatus.CANCELLED.value:
                raise conflict(
                    "invalid_state_transition",
                    "Only queued or running renders can be cancelled",
                    current_status=status,
                )
            self._store_idempotent_mutation(
                conn, idempotency_key, operation, render_key, request_hash, result
            )
        return result

    def retry_render(self, render_id: UUID | str, idempotency_key: str) -> JsonObject:
        render_key = _as_uuid(render_id)
        self._validate_idempotency_key(idempotency_key)
        operation = "retry_render"
        request_hash = _request_hash({"operation": operation, "render_id": render_key})
        with self.database.transaction() as conn:
            replay = self._get_idempotent_mutation(
                conn, idempotency_key, operation, render_key, request_hash
            )
            if replay is not None:
                return replay
            row = self._get_row(conn, "render_jobs", render_key)
            if row is None:
                raise not_found("Render job")
            current = _payload(row)
            if current["status"] != RunStatus.FAILED.value:
                raise conflict(
                    "invalid_state_transition",
                    "Only a failed render can be retried",
                    current_status=current["status"],
                )
            now = _utc_now()
            next_attempt = current["attempt"] + 1
            result = {
                **current,
                "status": RunStatus.QUEUED.value,
                "attempt": next_attempt,
                "cancel_requested_at": None,
                "output_ref": None,
                "error_message": None,
                "updated_at": now,
            }
            conn.execute(
                "UPDATE render_jobs SET status = ?, attempt = ?, cancel_requested_at = NULL, "
                "cancel_reason = NULL, execution_claimed_at = NULL, "
                "updated_at = ?, payload = ? WHERE id = ? AND owner_user_id = ? "
                "AND status = 'failed'",
                (
                    result["status"],
                    next_attempt,
                    now,
                    _json(result),
                    render_key,
                    self.owner_user_id,
                ),
            )
            self._store_idempotent_mutation(
                conn, idempotency_key, operation, render_key, request_hash, result
            )
        return result

    def is_render_cancel_requested(self, render_id: UUID | str) -> bool:
        return self.get_render(render_id).get("cancel_requested_at") is not None

    def confirm_render_cancelled(self, render_id: UUID | str) -> JsonObject:
        render_key = _as_uuid(render_id)
        with self.database.transaction() as conn:
            row = self._get_row(conn, "render_jobs", render_key)
            if row is None:
                raise not_found("Render job")
            current = _payload(row)
            if current["status"] == RunStatus.CANCELLED.value:
                return current
            if (
                current["status"] != RunStatus.RUNNING.value
                or current.get("cancel_requested_at") is None
            ):
                raise conflict(
                    "invalid_state_transition",
                    "Only a running render with a cancellation request can confirm cancellation",
                )
            now = _utc_now()
            result = {
                **current,
                "status": RunStatus.CANCELLED.value,
                "output_ref": None,
                "error_message": None,
                "updated_at": now,
            }
            conn.execute(
                "UPDATE render_jobs SET status = ?, execution_claimed_at = NULL, "
                "updated_at = ?, payload = ? "
                "WHERE id = ? AND owner_user_id = ?",
                (
                    result["status"],
                    now,
                    _json(result),
                    render_key,
                    self.owner_user_id,
                ),
            )
        return result

    def get_render_download_path(self, render_id: UUID | str) -> Path:
        """Resolve an owner-scoped completed MP4 beneath the configured artifact root."""

        render = self.get_render(render_id)
        if render["status"] != RunStatus.SUCCEEDED.value:
            raise conflict("render_not_complete", "Only a completed render can be downloaded")
        output_ref = render.get("output_ref")
        if not output_ref:
            raise conflict("render_output_missing", "Completed render has no output file")
        path, _ = self._resolve_render_output(output_ref, require_exists=True)
        return path

    def transition_render(
        self,
        render_id: UUID | str,
        *,
        status: str,
        output_ref: str | None = None,
        error_message: str | None = None,
    ) -> JsonObject:
        render_key = _as_uuid(render_id)
        requested_status = status
        if requested_status == RunStatus.SUCCEEDED.value and not output_ref:
            raise DomainError(
                422, "render_output_required", "A successful render requires output_ref"
            )
        stored_output_ref = (
            self._resolve_render_output(output_ref, require_exists=True)[1]
            if requested_status == RunStatus.SUCCEEDED.value and output_ref
            else None
        )
        with self.database.transaction() as conn:
            row = self._get_row(conn, "render_jobs", render_key)
            if row is None:
                raise not_found("Render job")
            current = _payload(row)
            if current.get("cancel_requested_at") and requested_status != RunStatus.CANCELLED.value:
                raise conflict(
                    "invalid_state_transition",
                    "A cancellation-requested render can only be confirmed as cancelled",
                )
            if (
                requested_status == RunStatus.CANCELLED.value
                and current["status"] == RunStatus.RUNNING.value
                and current.get("cancel_requested_at") is None
            ):
                raise conflict(
                    "invalid_state_transition",
                    "A running render must have a cancellation request before cancellation",
                )
            target_status = requested_status
            target_error = error_message
            if requested_status in {
                RunStatus.RUNNING.value,
                RunStatus.SUCCEEDED.value,
            }:
                candidate = self._get(conn, "promotion_candidates", row["promotion_candidate_id"])
                if candidate is None or candidate.get("eligibility_status") != MANUALLY_CONFIRMED:
                    target_status = RunStatus.FAILED.value
                    target_error = (
                        "Promotion eligibility must be manually confirmed before rendering"
                    )
                    stored_output_ref = None
            validate_render_transition(current["status"], target_status)
            if current["status"] == target_status:
                if target_status == RunStatus.SUCCEEDED.value and current.get("output_ref") == output_ref:
                    return current
                if target_status == RunStatus.FAILED.value and current.get("error_message") == target_error:
                    return current
                raise conflict("render_already_finished", "Render job is already in this state")
            now = _utc_now()
            updated = {
                **current,
                "status": target_status,
                "output_ref": stored_output_ref,
                "error_message": target_error if target_status == RunStatus.FAILED.value else None,
                "updated_at": now,
            }
            conn.execute(
                "UPDATE render_jobs SET status = ?, execution_claimed_at = ?, "
                "updated_at = ?, payload = ? "
                "WHERE id = ? AND owner_user_id = ?",
                (
                    target_status,
                    now if target_status == RunStatus.RUNNING.value else None,
                    now,
                    _json(updated),
                    render_key,
                    self.owner_user_id,
                ),
            )
            if target_status == RunStatus.SUCCEEDED.value:
                self._create_publication_package(conn, row, updated)
        return updated

    def _resolve_render_output(
        self,
        output_ref: str,
        *,
        require_exists: bool,
    ) -> tuple[Path, str]:
        root = self.render_output_dir.resolve()
        candidate = Path(output_ref).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve(strict=require_exists)
        except (OSError, RuntimeError) as exc:
            raise conflict("render_output_missing", "Render output file does not exist") from exc
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise conflict(
                "render_output_outside_root",
                "Render output must be inside the configured artifact directory",
            ) from exc
        if resolved.suffix.lower() != ".mp4":
            raise conflict("render_output_invalid", "Render output must be an MP4 file")
        if require_exists and not resolved.is_file():
            raise conflict("render_output_missing", "Render output file does not exist")
        return resolved, relative.as_posix()

    def complete_render(self, render_id: UUID | str, output_ref: str) -> JsonObject:
        return self.transition_render(
            render_id,
            status=RunStatus.SUCCEEDED.value,
            output_ref=output_ref,
        )

    def fail_render(self, render_id: UUID | str, error_message: str) -> JsonObject:
        return self.transition_render(
            render_id,
            status=RunStatus.FAILED.value,
            error_message=error_message,
        )

    def get_publication_package(self, project_id: UUID | str) -> JsonObject:
        package, _render_id = self.get_publication_package_record(project_id)
        return package

    def get_publication_package_record(
        self, project_id: UUID | str
    ) -> tuple[JsonObject, str]:
        """Return persisted package metadata plus its private render ID for URL building."""

        project_key = _as_uuid(project_id)
        with self.database.read() as conn:
            if self._get(conn, "creative_projects", project_key) is None:
                raise not_found("Creative project")
            row = conn.execute(
                "SELECT payload, render_id FROM publication_packages WHERE project_id = ? "
                "AND owner_user_id = ? ORDER BY created_at DESC LIMIT 1",
                (project_key, self.owner_user_id),
            ).fetchone()
            if row is None:
                raise DomainError(
                    404,
                    "publication_package_not_ready",
                    "A publication package is available after an approved render completes",
                )
            render_row = self._get_row(conn, "render_jobs", row["render_id"])
            if render_row is None:
                raise conflict(
                    "package_references_missing", "Package render is unavailable"
                )
            return (
                self._refresh_publication_checklist(conn, _payload(row), render_row),
                row["render_id"],
            )

    def save_publication_package(
        self,
        project_id: UUID | str,
        render_id: UUID | str | None = None,
    ) -> JsonObject:
        """Persist and return manual publication metadata for a completed render."""

        project_key = _as_uuid(project_id)
        render_key = _as_uuid(render_id) if render_id else None
        with self.database.transaction() as conn:
            if self._get(conn, "creative_projects", project_key) is None:
                raise not_found("Creative project")
            if render_key:
                row = self._get_row(conn, "render_jobs", render_key)
            else:
                row = conn.execute(
                    "SELECT * FROM render_jobs WHERE project_id = ? AND owner_user_id = ? "
                    "AND status = 'succeeded' ORDER BY updated_at DESC LIMIT 1",
                    (project_key, self.owner_user_id),
                ).fetchone()
            if row is None:
                raise not_found("Completed render job")
            render = _payload(row)
            if render["project_id"] != project_key:
                raise not_found("Completed render job")
            if render["status"] != RunStatus.SUCCEEDED.value:
                raise conflict(
                    "render_not_complete",
                    "A publication package requires a completed render",
                )
            return self._create_publication_package(conn, row, render)

    def _validate_approval(
        self,
        conn: Any,
        project: JsonObject,
        review_data: JsonObject,
    ) -> None:
        for required_flag in (
            "fact_confirmed",
            "product_match_confirmed",
            "asset_rights_confirmed",
        ):
            if review_data.get(required_flag) is not True:
                raise conflict(
                    "review_confirmation_required",
                    f"{required_flag} must be true before approval",
                    field=required_flag,
                )
        book = self._get(conn, "books", project["book_id"])
        candidate = self._get(conn, "promotion_candidates", project["promotion_candidate_id"])
        if book is None or candidate is None:
            raise conflict("project_references_missing", "Project references are unavailable")

        for fact in book.get("facts", []):
            if fact["verification_status"] == "unverified":
                raise conflict(
                    "book_fact_unverified",
                    "All facts must be source-verified or human-confirmed before approval",
                    fact_key=fact["key"],
                )
            if fact["verification_status"] == "source_verified":
                if not fact.get("source_note") and not fact.get("evidence_id"):
                    raise conflict(
                        "book_fact_source_missing",
                        "Source-verified facts require a source note or linked evidence",
                        fact_key=fact["key"],
                    )
            evidence_id = fact.get("evidence_id")
            if evidence_id and self._get(conn, "evidence", evidence_id) is None:
                raise conflict(
                    "book_fact_evidence_missing",
                    "Linked fact evidence is not available to this owner",
                    fact_key=fact["key"],
                )
        if candidate["eligibility_status"] != MANUALLY_CONFIRMED:
            raise conflict(
                "product_not_confirmed",
                "Promotion eligibility must be manually checked before approval",
            )
        checked_at = candidate.get("eligibility_checked_at")
        if not checked_at or not candidate.get("eligibility_note"):
            raise conflict(
                "product_check_details_missing",
                "Manual product check time and note are required before approval",
            )
        if _timestamp(checked_at) > datetime.now(timezone.utc):
            raise conflict(
                "product_check_time_invalid",
                "Manual product check time cannot be in the future",
            )

    @staticmethod
    def _script_content(script: JsonObject) -> JsonObject:
        return {
            "based_on_version_id": script.get("based_on_version_id"),
            "segments": script["segments"],
            "title_suggestion": script["title_suggestion"],
            "description_suggestion": script["description_suggestion"],
        }

    @staticmethod
    def _fact_revision_refs(book: JsonObject, script_data: JsonObject) -> list[JsonObject]:
        used_keys = {key for segment in script_data["segments"] for key in segment["fact_keys"]}
        return [
            {"key": fact["key"], "revision": int(fact.get("revision", 1))}
            for fact in book.get("facts", []) if fact["key"] in used_keys
        ]

    @staticmethod
    def _validate_script_fact_revisions(book: JsonObject | None, script: JsonObject) -> None:
        if book is None:
            raise not_found("Book edition")
        current = {fact["key"]: fact for fact in book.get("facts", [])}
        for ref in script.get("fact_revision_refs", []):
            fact = current.get(ref["key"])
            if fact is None or fact["verification_status"] == "unverified":
                raise conflict("book_fact_unverified", "A script fact was withdrawn or is unverified", fact_key=ref["key"])

    def _validate_script_content(
        self,
        conn: Any,
        project: JsonObject,
        book: JsonObject | None,
        script_data: JsonObject,
    ) -> None:
        if book is None:
            raise not_found("Book edition")
        known_fact_keys = {fact["key"] for fact in book.get("facts", [])}
        unknown_fact_keys = sorted(
            {
                fact_key
                for segment in script_data["segments"]
                for fact_key in segment["fact_keys"]
                if fact_key not in known_fact_keys
            }
        )
        if unknown_fact_keys:
            raise conflict(
                "script_fact_key_unknown",
                "Script fact_keys must reference facts entered for this book edition",
                fact_keys=unknown_fact_keys,
            )
        total_duration = sum(
            float(segment["duration_seconds"]) for segment in script_data["segments"]
        )
        if not 20 <= total_duration <= 45:
            raise conflict(
                "script_duration_out_of_range",
                "Script duration must total between 20 and 45 seconds",
                duration_seconds=total_duration,
            )
        expected_duration = float(project["desired_seconds"])
        if abs(total_duration - expected_duration) > 0.05:
            raise conflict(
                "script_duration_mismatch",
                "Script segment durations must sum to project.desired_seconds",
                expected_seconds=expected_duration,
                actual_seconds=total_duration,
            )

    def _validate_asset_rights(self, rights: Any) -> JsonObject:
        item = _dump(rights)
        if item["usage_status"] != "allowed":
            raise conflict(
                "asset_rights_blocked",
                "Every render asset must have confirmed allowed usage rights",
            )
        if item["origin"] == "licensed":
            if not item.get("license_basis") or not item.get("proof_ref"):
                raise conflict(
                    "asset_license_incomplete",
                    "Licensed assets require a license basis and proof reference",
                )
            expires_at = item.get("expires_at")
            if not expires_at or _timestamp(expires_at) <= datetime.now(timezone.utc):
                raise conflict(
                    "asset_license_expired",
                    "Licensed assets require an unexpired usage grant",
                )
            permitted_uses = {str(value).strip().lower() for value in item["permitted_uses"]}
            has_video = bool(permitted_uses & {"video", "commercial_video", "commercial video"})
            has_commercial = bool(
                permitted_uses & {"commercial", "commercial_video", "commercial use video"}
            )
            if not (has_video and has_commercial):
                raise conflict(
                    "asset_license_scope_missing",
                    "Licensed assets must explicitly permit commercial video use",
                )
        return item

    def _refresh_publication_checklist(
        self, conn: Any, package: JsonObject, render_row: Any
    ) -> JsonObject:
        project = self._get(conn, "creative_projects", package["project_id"])
        candidate = self._get(
            conn, "promotion_candidates", package["promotion_candidate_id"]
        )
        if project is None or candidate is None:
            raise conflict("package_references_missing", "Package references are unavailable")
        rights = json.loads(render_row["asset_rights"])
        checklist = {
            "account_eligible": False,
            "product_eligible": candidate.get("eligibility_status") == MANUALLY_CONFIRMED,
            "edition_matches": candidate.get("book_id") == project.get("book_id"),
            "rights_confirmed": bool(rights)
            and all(item.get("usage_status") == "allowed" for item in rights),
            "manual_attachment_required": True,
        }
        return {**package, "checklist": checklist, "publication_state": "needs_manual_checks"}

    def _create_publication_package(
        self, conn: Any, render_row: Any, render: JsonObject
    ) -> JsonObject:
        existing = conn.execute(
            "SELECT payload FROM publication_packages WHERE project_id = ? "
            "AND script_version_id = ? AND render_id = ? AND owner_user_id = ?",
            (
                render["project_id"],
                render["script_version_id"],
                render["id"],
                self.owner_user_id,
            ),
        ).fetchone()
        if existing:
            return _payload(existing)
        project = self._get(conn, "creative_projects", render["project_id"])
        book = self._get(conn, "books", project["book_id"]) if project else None
        script = self._get(conn, "script_versions", render["script_version_id"])
        candidate_id = self._get_candidate_id_for_render(conn, render["id"])
        candidate = self._get(conn, "promotion_candidates", candidate_id)
        if not project or not book or not script or not candidate:
            raise conflict("package_references_missing", "Render references are unavailable")
        rights = json.loads(render_row["asset_rights"])
        checklist = {
            "account_eligible": False,
            "product_eligible": candidate["eligibility_status"] == MANUALLY_CONFIRMED,
            "edition_matches": candidate["book_id"] == project["book_id"],
            "rights_confirmed": bool(rights)
            and all(item.get("usage_status") == "allowed" for item in rights),
            "manual_attachment_required": True,
        }
        segments = script.get("segments", [])
        now = _utc_now()
        package = {
            "id": str(uuid4()),
            "project_id": project["id"],
            "book_id": book["id"],
            "script_version_id": script["id"],
            "promotion_candidate_id": candidate["id"],
            "render_id": render["id"],
            "title": script["title_suggestion"],
            "description": script["description_suggestion"],
            "cover_text": segments[0]["onscreen_text"] if segments else "",
            "publication_state": "needs_manual_checks",
            "checklist": checklist,
        }
        conn.execute(
            "INSERT INTO publication_packages"
            "(id, owner_user_id, project_id, script_version_id, render_id, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                package["id"],
                self.owner_user_id,
                project["id"],
                script["id"],
                render["id"],
                now,
                _json(package),
            ),
        )
        return package

    def _get_candidate_id_for_render(self, conn: Any, render_id: str) -> str | None:
        row = conn.execute(
            "SELECT promotion_candidate_id FROM render_jobs WHERE id = ? "
            "AND owner_user_id = ?",
            (render_id, self.owner_user_id),
        ).fetchone()
        return row["promotion_candidate_id"] if row else None

    def _get_or_404(self, table: str, record_id: UUID | str, resource: str) -> JsonObject:
        key = _as_uuid(record_id)
        with self.database.read() as conn:
            result = self._get(conn, table, key)
        if result is None:
            raise not_found(resource)
        return result

    def _get(self, conn: Any, table: str, record_id: UUID | str) -> JsonObject | None:
        row = self._get_row(conn, table, record_id)
        return _payload(row) if row else None

    def _get_row(self, conn: Any, table: str, record_id: UUID | str) -> Any:
        if table not in {
            "books",
            "promotion_candidates",
            "evidence",
            "creative_projects",
            "script_versions",
            "runs",
            "render_jobs",
            "audio_assets",
            "generation_results",
            "model_cost_records",
            "metric_observations",
            "content_references",
            "style_cards",
            "import_jobs",
        }:
            raise ValueError(f"Unsupported table {table}")
        return conn.execute(
            f"SELECT * FROM {table} WHERE id = ? AND owner_user_id = ?",
            (_as_uuid(record_id), self.owner_user_id),
        ).fetchone()

    def _run_event(
        self,
        *,
        run: JsonObject,
        event_type: str,
        occurred_at: str,
        sequence: int,
        script_version_id: str | None = None,
        render_id: str | None = None,
        reason_code: str | None = None,
        message: str | None = None,
        attempt: int | None = None,
    ) -> JsonObject:
        event = {
            "event_id": str(uuid4()),
            "run_id": run["id"],
            "project_id": run["project_id"],
            "sequence": sequence,
            "type": event_type,
            "status": run["status"],
            "stage": run["stage"],
            "occurred_at": occurred_at,
            "script_version_id": script_version_id,
            "render_id": render_id,
            "reason_code": reason_code,
            "message": message,
        }
        if attempt is not None:
            event["attempt"] = attempt
        return event

    def _insert_event(self, conn: Any, event: JsonObject) -> None:
        conn.execute(
            "INSERT INTO run_events"
            "(event_id, owner_user_id, run_id, sequence, occurred_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                event["event_id"],
                self.owner_user_id,
                event["run_id"],
                event["sequence"],
                event["occurred_at"],
                _json(event),
            ),
        )

    @staticmethod
    def _event_type_for_status(status: str) -> str:
        return {
            RunStatus.WAITING_REVIEW.value: RunEventType.REVIEW_REQUESTED.value,
            RunStatus.SUCCEEDED.value: RunEventType.COMPLETED.value,
            RunStatus.FAILED.value: RunEventType.FAILED.value,
            RunStatus.CANCELLED.value: RunEventType.CANCELLED.value,
        }.get(status, RunEventType.STAGE_CHANGED.value)

    @staticmethod
    def _validate_event_type(
        event_type: str, status: str, attempt: int | None = None
    ) -> None:
        try:
            parsed = RunEventType(event_type)
        except ValueError as exc:
            raise DomainError(422, "invalid_event_type", "Unknown run event type") from exc
        required_status = {
            RunEventType.REVIEW_REQUESTED: RunStatus.WAITING_REVIEW.value,
            RunEventType.FAILED: RunStatus.FAILED.value,
            RunEventType.COMPLETED: RunStatus.SUCCEEDED.value,
            RunEventType.CANCEL_REQUESTED: RunStatus.RUNNING.value,
            RunEventType.CANCELLED: RunStatus.CANCELLED.value,
            RunEventType.RETRY_REQUESTED: RunStatus.QUEUED.value,
        }.get(parsed)
        if required_status and status != required_status:
            raise conflict(
                "event_status_mismatch",
                f"{event_type} requires status {required_status}",
            )
        if parsed in {RunEventType.CANCEL_REQUESTED, RunEventType.RETRY_REQUESTED,
                      RunEventType.GENERATION_COMPLETED, RunEventType.GENERATION_FAILED,
                      RunEventType.COST_RECORDED}:
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                raise DomainError(
                    422,
                    "invalid_run_event",
                    f"{event_type} requires a positive attempt number",
                )
            if parsed == RunEventType.RETRY_REQUESTED and attempt < 2:
                raise DomainError(
                    422,
                    "invalid_run_event",
                    "run.retry_requested requires attempt 2 or greater",
                )

    @staticmethod
    def _validate_idempotency_key(key: str) -> None:
        if not 8 <= len(key) <= 128:
            raise DomainError(
                422,
                "invalid_idempotency_key",
                "Idempotency-Key length must be between 8 and 128 characters",
            )

    def _get_idempotent_mutation(
        self,
        conn: Any,
        key: str,
        operation: str,
        resource_id: str,
        request_hash: str,
    ) -> JsonObject | None:
        row = conn.execute(
            "SELECT operation, resource_id, request_hash, response_payload "
            "FROM idempotency_operations WHERE owner_user_id = ? AND idempotency_key = ?",
            (self.owner_user_id, key),
        ).fetchone()
        if row is None:
            return None
        if (
            row["operation"] != operation
            or row["resource_id"] != resource_id
            or row["request_hash"] != request_hash
        ):
            raise conflict(
                "idempotency_key_conflict",
                "Idempotency-Key was already used with a different request",
            )
        return json.loads(row["response_payload"])

    def _store_idempotent_mutation(
        self,
        conn: Any,
        key: str,
        operation: str,
        resource_id: str,
        request_hash: str,
        response: JsonObject,
    ) -> None:
        conn.execute(
            "INSERT INTO idempotency_operations"
            "(owner_user_id, idempotency_key, operation, resource_id, request_hash, "
            "response_payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                self.owner_user_id,
                key,
                operation,
                resource_id,
                request_hash,
                _json(response),
                _utc_now(),
            ),
        )

    @staticmethod
    def _validate_queue_limit(limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise DomainError(422, "invalid_limit", "Queue limit must be between 1 and 500")

    @staticmethod
    def _validate_run_lease(lease_seconds: int) -> None:
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 3600
        ):
            raise DomainError(
                422,
                "invalid_lease",
                "Execution lease must be between 1 and 3600 seconds",
            )
