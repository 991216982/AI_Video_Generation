"""Pydantic request and response models matching contracts/openapi.yaml."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import math
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Platform(str, Enum):
    DOUYIN = "douyin"
    KUAISHOU = "kuaishou"


class VerificationStatus(str, Enum):
    UNVERIFIED = "unverified"
    SOURCE_VERIFIED = "source_verified"
    HUMAN_CONFIRMED = "human_confirmed"


class BookFact(StrictModel):
    key: str = Field(min_length=1)
    value: str = Field(min_length=1)
    verification_status: VerificationStatus
    source_note: str | None = None
    evidence_id: UUID | None = None


class BookCreate(StrictModel):
    isbn: str | None = None
    title: str = Field(min_length=1)
    edition_label: str = Field(min_length=1)
    author: str = Field(min_length=1)
    publisher: str | None = None
    original_opinion: str = Field(min_length=1)
    facts: list[BookFact]


class BookEdition(BookCreate):
    id: UUID
    owner_user_id: UUID
    created_at: datetime


class EligibilityStatus(str, Enum):
    UNVERIFIED = "unverified"
    MANUALLY_CONFIRMED = "manually_confirmed"


class PromotionCandidateCreate(StrictModel):
    book_id: UUID
    platform: Platform
    platform_product_id: str = Field(min_length=1)
    product_url: str = Field(min_length=1)
    eligibility_status: EligibilityStatus = EligibilityStatus.UNVERIFIED
    eligibility_checked_at: datetime | None = None
    eligibility_note: str | None = None

    @field_validator("product_url")
    @classmethod
    def product_url_must_be_a_web_uri(cls, value: str) -> str:
        _validate_web_uri(value, "product_url")
        return value

    @field_validator("eligibility_checked_at")
    @classmethod
    def checked_at_must_include_timezone(
        cls, value: datetime | None
    ) -> datetime | None:
        return _timezone_aware(value)

    @model_validator(mode="after")
    def confirmed_eligibility_has_manual_check(self) -> PromotionCandidateCreate:
        if self.eligibility_status == EligibilityStatus.MANUALLY_CONFIRMED:
            if self.eligibility_checked_at is None or not self.eligibility_note:
                raise ValueError(
                    "manual confirmation requires eligibility_checked_at and eligibility_note"
                )
        return self


class PromotionCandidate(PromotionCandidateCreate):
    id: UUID
    owner_user_id: UUID
    created_at: datetime


class EvidenceCreate(StrictModel):
    source_type: Literal["manual", "licensed"]
    source_note: str = Field(min_length=1)
    source_url: str | None = None
    observed_at: datetime
    usage_basis: str = Field(min_length=1)
    metric_name: str | None = None
    metric_value: float | None = None
    metric_window_start: datetime | None = None
    metric_window_end: datetime | None = None
    missing_reason: str | None = None

    @field_validator("source_url")
    @classmethod
    def source_url_must_be_a_web_uri(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_web_uri(value, "source_url")
        return value

    @field_validator("metric_value")
    @classmethod
    def metric_value_must_be_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("metric_value must be finite")
        return value

    @field_validator("observed_at", "metric_window_start", "metric_window_end")
    @classmethod
    def timestamps_must_include_timezone(
        cls, value: datetime | None
    ) -> datetime | None:
        return _timezone_aware(value)

    @model_validator(mode="after")
    def missing_metrics_need_a_reason(self) -> EvidenceCreate:
        if self.metric_value is None and not self.missing_reason:
            raise ValueError("missing_reason is required when metric_value is absent")
        if (
            self.metric_window_start is not None
            and self.metric_window_end is not None
            and self.metric_window_start > self.metric_window_end
        ):
            raise ValueError("metric_window_start must not follow metric_window_end")
        return self


class Evidence(EvidenceCreate):
    id: UUID
    owner_user_id: UUID
    collected_at: datetime


class CreativeProjectCreate(StrictModel):
    book_id: UUID
    promotion_candidate_id: UUID
    target_audience: str = Field(min_length=1)
    target_platform: Platform
    desired_seconds: int = Field(ge=20, le=45)
    original_angle: str = Field(min_length=1)
    prohibited_claims: list[str] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)


class CreativeProject(CreativeProjectCreate):
    id: UUID
    owner_user_id: UUID
    created_at: datetime


class ScriptSegment(StrictModel):
    order: int = Field(ge=0)
    onscreen_text: str
    narration: str
    fact_keys: list[str]
    duration_seconds: float = Field(gt=0)


class ScriptVersionCreate(StrictModel):
    based_on_version_id: UUID | None = None
    segments: list[ScriptSegment] = Field(min_length=1)
    title_suggestion: str
    description_suggestion: str

    @model_validator(mode="after")
    def segment_order_is_unique(self) -> ScriptVersionCreate:
        orders = [segment.order for segment in self.segments]
        if len(orders) != len(set(orders)):
            raise ValueError("script segment order values must be unique")
        return self


class ScriptVersion(ScriptVersionCreate):
    id: UUID
    project_id: UUID
    owner_user_id: UUID
    version_number: int = Field(ge=1)
    review_status: Literal["pending", "approved", "rejected"]
    created_at: datetime


class RunCreate(StrictModel):
    brief_version: int = Field(ge=1)


class Run(StrictModel):
    id: UUID
    project_id: UUID
    owner_user_id: UUID
    status: Literal[
        "queued", "running", "waiting_review", "succeeded", "failed", "cancelled"
    ]
    stage: Literal[
        "prepare", "script", "fact_check", "review", "storyboard", "render", "complete"
    ]
    sequence: int = Field(ge=0)
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ReviewRequest(StrictModel):
    script_version_id: UUID
    decision: Literal["approve", "reject"]
    note: str | None = None
    fact_confirmed: bool
    product_match_confirmed: bool
    asset_rights_confirmed: bool


class AssetRights(StrictModel):
    origin: Literal[
        "self_created_text",
        "self_created_graphic",
        "self_recorded_voice",
        "licensed",
    ]
    usage_status: Literal["allowed", "blocked"]
    license_basis: str | None = None
    permitted_uses: list[str] = Field(default_factory=list)
    proof_ref: str | None = None
    expires_at: datetime | None = None

    @field_validator("expires_at")
    @classmethod
    def expiry_must_include_timezone(cls, value: datetime | None) -> datetime | None:
        return _timezone_aware(value)


class RenderCreate(StrictModel):
    project_id: UUID
    script_version_id: UUID
    promotion_candidate_id: UUID
    width: Literal[1080]
    height: Literal[1920]
    asset_rights: list[AssetRights] = Field(min_length=1)


class RenderJob(StrictModel):
    id: UUID
    project_id: UUID
    script_version_id: UUID
    owner_user_id: UUID
    status: Literal[
        "queued", "running", "waiting_review", "succeeded", "failed", "cancelled"
    ]
    output_ref: str | None = None
    error_message: str | None = None
    created_at: datetime


class PublicationChecklist(StrictModel):
    account_eligible: bool
    product_eligible: bool
    edition_matches: bool
    rights_confirmed: bool
    manual_attachment_required: Literal[True]


class PublicationPackage(StrictModel):
    id: UUID
    project_id: UUID
    book_id: UUID
    script_version_id: UUID
    promotion_candidate_id: UUID
    render_id: UUID
    title: str
    description: str
    cover_text: str | None = None
    video_download_url: str
    publication_state: Literal["needs_manual_checks", "ready_for_manual_publish"]
    checklist: PublicationChecklist


def _timezone_aware(value: datetime | None) -> datetime | None:
    if value is not None and (
        value.tzinfo is None or value.utcoffset() is None
    ):
        raise ValueError("timestamp must include a UTC offset")
    return value.astimezone(timezone.utc) if value is not None else None


def _validate_web_uri(value: str, field_name: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an absolute http(s) URI")
