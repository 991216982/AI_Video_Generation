"""Small, provider-neutral interface for structured script generation.

No network model is configured by default. Integrators may supply an adapter
only after its data handling, licensing and billing terms have been checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class ModelResponse:
    script: Mapping[str, Any]
    source: str
    provider_key: str
    model_key: str
    currency: str = "CNY"
    amount_minor: int | None = None
    missing_reason: str | None = "provider_billing_unavailable"
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelAdapterError(RuntimeError):
    """Provider failure with a stable public reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        if reason_code not in {"provider_unavailable", "provider_timeout", "invalid_output", "unsupported_claim", "missing_fact_basis", "prohibited_claim"}:
            raise ValueError("unsupported model failure reason")
        super().__init__(message)
        self.reason_code = reason_code


class ModelAdapter(Protocol):
    """Optional recovery hooks: lookup_result(key) and supports_request_idempotency.

    Only set supports_request_idempotency when the provider guarantees that
    repeating the same key returns the same logical invocation and bill.
    A lookup returning None is safe to retry only with that guarantee.
    """

    def generate(self, *, variant: str, context: Mapping[str, Any]) -> ModelResponse: ...


class DeterministicFixtureAdapter:
    """Use locally constructed original text without a key or provider call."""

    def generate(self, *, variant: str, context: Mapping[str, Any]) -> ModelResponse:
        scripts = context["fixture_scripts"]
        return ModelResponse(
            script=scripts[variant],
            source="deterministic_fixture",
            provider_key="local_fixture",
            model_key="fixture_v1",
            amount_minor=0,
            missing_reason=None,
            input_tokens=0,
            output_tokens=0,
        )
