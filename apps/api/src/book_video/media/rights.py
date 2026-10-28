"""Asset-rights checks shared by review and rendering entry points."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


class AssetRightsError(ValueError):
    """An asset's declared rights are blocked, expired, or incomplete."""


def _parse_expiry(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise AssetRightsError("expires_at must be an RFC 3339 timestamp or null")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise AssetRightsError("expires_at must be an RFC 3339 timestamp or null") from error
    if parsed.tzinfo is None:
        raise AssetRightsError("expires_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def asset_rights_blockers(
    rights: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    required_origins: Sequence[str] = (),
) -> list[str]:
    """Return stable blocker codes for declared assets without raising.

    Missing voice rights do not trigger external audio: the renderer always
    produces a silent file. Code-generated text and geometric shapes are
    self-created and are the only assets the renderer consumes.
    """

    blockers: list[str] = []
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not isinstance(rights, Sequence) or isinstance(rights, (str, bytes)):
        return ["asset_rights_invalid"]
    observed_origins = {
        str(item.get("origin"))
        for item in rights
        if isinstance(item, Mapping)
    }
    for origin in required_origins:
        if origin not in observed_origins:
            blockers.append(f"asset_rights_missing:{origin}")
    for index, item in enumerate(rights):
        if not isinstance(item, Mapping):
            blockers.append(f"asset_rights_invalid:{index}")
            continue
        origin = item.get("origin")
        status = item.get("usage_status")
        if origin not in {"self_created_text", "self_created_graphic", "self_recorded_voice", "licensed"}:
            blockers.append(f"asset_origin_invalid:{index}")
        if status != "allowed":
            blockers.append(f"asset_usage_blocked:{index}")
        try:
            expiry = _parse_expiry(item.get("expires_at"))
        except AssetRightsError:
            blockers.append(f"asset_expiry_invalid:{index}")
            expiry = None
        if expiry is not None and expiry <= clock:
            blockers.append(f"asset_rights_expired:{index}")
        if origin == "licensed":
            if not str(item.get("license_basis", "")).strip():
                blockers.append(f"license_basis_missing:{index}")
            if not str(item.get("proof_ref", "")).strip():
                blockers.append(f"license_proof_missing:{index}")
            permitted = item.get("permitted_uses", [])
            if not isinstance(permitted, list) or not permitted:
                blockers.append(f"license_scope_missing:{index}")
    return blockers


def validate_asset_rights(
    rights: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    required_origins: Sequence[str] = (),
) -> None:
    blockers = asset_rights_blockers(rights, now=now, required_origins=required_origins)
    if blockers:
        raise AssetRightsError("asset rights check failed: " + ", ".join(blockers))
