"""Application errors translated to the shared API error envelope."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def not_found(resource: str) -> DomainError:
    return DomainError(404, "not_found", f"{resource} was not found")


def conflict(code: str, message: str, **details: Any) -> DomainError:
    return DomainError(409, code, message, details=details or None)
