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
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, protected_namespaces=())


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
    revision: int = Field(default=1, ge=1)


class BookFactRevisionRequest(StrictModel):
    expected_revision: int = Field(ge=1)
    value: str = Field(min_length=1)
    verification_status: VerificationStatus
    source_note: str | None = Field(default=None, max_length=1000)
    evidence_id: UUID | None = None


class BookFactRevision(BookFact):
    book_id: UUID
    created_at: datetime
    created_by_user_id: UUID


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


class BookPage(StrictModel):
    items: list[BookEdition]
    next_cursor: str | None


class EligibilityStatus(str, Enum):
    UNVERIFIED = "unverified"
    MANUALLY_CONFIRMED = "manually_confirmed"


class PromotionCandidateCreate(StrictModel):
    book_id: UUID
    platform: Platform
    platform_product_id: str = Field(min_length=1)
    product_url: str = Field(min_length=1)
    eligibility_status: Literal[EligibilityStatus.UNVERIFIED] = EligibilityStatus.UNVERIFIED
    eligibility_checked_at: None = None
    eligibility_note: str | None = None

    @field_validator("product_url")
    @classmethod
    def product_url_must_be_a_web_uri(cls, value: str) -> str:
        _validate_web_uri(value, "product_url")
        return value

class PromotionCandidate(PromotionCandidateCreate):
    eligibility_status: EligibilityStatus
    eligibility_checked_at: datetime | None = None
    id: UUID
    owner_user_id: UUID
    created_at: datetime
    eligibility_revision: int = Field(ge=1)
    eligibility_checked_by_user_id: UUID | None


class PromotionEligibilityUpdate(StrictModel):
    eligibility_status: EligibilityStatus
    expected_revision: int = Field(ge=1)
    eligibility_note: str | None = Field(max_length=500)
    confirmation_basis: Literal["creator_account_backend"] | None

    @model_validator(mode="after")
    def match_manual_confirmation_fields(self) -> PromotionEligibilityUpdate:
        if self.eligibility_status == EligibilityStatus.MANUALLY_CONFIRMED:
            if not self.eligibility_note or self.confirmation_basis != "creator_account_backend":
                raise ValueError(
                    "manual confirmation requires a note and creator_account_backend basis"
                )
        elif self.confirmation_basis is not None:
            raise ValueError("unverified eligibility cannot include a confirmation basis")
        return self


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
    tone_instruction: str | None = None


class CreativeProject(CreativeProjectCreate):
    id: UUID
    owner_user_id: UUID
    created_at: datetime


class CreativeProjectPage(StrictModel):
    items: list[CreativeProject]
    next_cursor: str | None


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
    generation_result_id: UUID | None = None
    fact_revision_refs: list[dict[str, int | str]] = Field(default_factory=list)
    created_at: datetime


class AudioAsset(StrictModel):
    id: UUID
    owner_user_id: UUID
    origin: Literal["self_recorded_voice", "licensed_voice"]
    sha256: str
    mime_type: Literal["audio/wav", "audio/mpeg", "audio/mp4"]
    duration_ms: int = Field(ge=1, le=45000)
    rights_attested: bool
    rights_proof_note: str
    permitted_uses: Literal["render_and_manual_publish"]
    expires_at: datetime | None
    usage_status: Literal["allowed", "blocked"]
    created_at: datetime


class GenerationResult(StrictModel):
    id: UUID
    run_id: UUID
    owner_user_id: UUID
    attempt: int = Field(ge=1)
    invocation_key: str
    source: Literal["deterministic_fixture", "external_model"]
    provider_key: str
    model_key: str
    status: Literal["succeeded", "failed"]
    reason_code: str | None
    input_digest: str
    output_digest: str | None
    script_version_id: UUID | None
    created_at: datetime


class ModelCostRecord(StrictModel):
    id: UUID
    run_id: UUID
    owner_user_id: UUID
    generation_result_id: UUID
    currency: str
    amount_minor: int | None = Field(ge=0)
    missing_reason: str | None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    recorded_at: datetime


class RunCreate(StrictModel):
    brief_version: int = Field(ge=1)


class CancelRequest(StrictModel):
    reason: str | None = Field(max_length=500)


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
    attempt: int = Field(ge=1)
    cancel_requested_at: datetime | None
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
    audio_asset_id: UUID | None = None
    asset_rights: list[AssetRights] = Field(min_length=1)


class RenderJob(StrictModel):
    id: UUID
    project_id: UUID
    script_version_id: UUID
    audio_asset_id: UUID | None = None
    owner_user_id: UUID
    status: Literal[
        "queued", "running", "waiting_review", "succeeded", "failed", "cancelled"
    ]
    attempt: int = Field(ge=1)
    cancel_requested_at: datetime | None
    output_ref: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


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


class CreativeWorkspace(StrictModel):
    project: CreativeProject
    book: BookEdition
    promotion_candidate: PromotionCandidate
    evidence: list[Evidence]
    script_versions: list[ScriptVersion]
    runs: list[Run]
    render_jobs: list[RenderJob]
    publication_package: PublicationPackage | None


# --- W4-A: research desk, metric evidence, content references, style cards, CSV import ---

class EvidencePage(StrictModel):
    items: list[Evidence]
    next_cursor: str | None


MetricBasis = Literal[
    "authorized_order_sales",
    "platform_listing_display",
    "third_party_or_manual_estimate",
    "engagement_signal",
    "legacy_unclassified",
]
MetricName = Literal[
    "sales_units",
    "sales_amount",
    "listed_price",
    "displayed_sales_label",
    "plays",
    "likes",
    "comments",
    "shares",
    "favorites",
    "follower_count",
    "ranking_position",
    "search_heat",
    "other",
]
MetricUnit = Literal[
    "count",
    "minor_currency",
    "currency_yuan",
    "views",
    "likes",
    "percent",
    "rank",
    "score",
    "text",
    "unspecified",
]
ObjectType = Literal["product_listing", "creator_account", "content_item", "book_edition"]

# Bases that describe a measured window; legacy_unclassified and platform display labels
# may legitimately lack a bounded window until re-validated.
_WINDOWED_BASES = {
    "authorized_order_sales",
    "third_party_or_manual_estimate",
    "engagement_signal",
}


class MetricObservationCreate(StrictModel):
    platform: Platform | None = None
    book_id: UUID | None = None
    object_type: ObjectType
    object_ref: str = Field(min_length=1, max_length=200)
    object_label: str | None = Field(default=None, max_length=200)
    metric_name: MetricName
    metric_definition: str = Field(min_length=1, max_length=500)
    basis: MetricBasis
    unit: MetricUnit
    value: float | None = None
    value_text: str | None = Field(default=None, max_length=100)
    window_start: datetime | None = None
    window_end: datetime | None = None
    observed_at: datetime
    evidence_id: UUID | None = None
    missing_reason: str | None = Field(default=None, max_length=500)
    confidence: Literal["high", "medium", "low"] | None = None
    expires_at: datetime | None = None
    note: str | None = Field(default=None, max_length=1000)

    @field_validator("value")
    @classmethod
    def value_must_be_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("metric value must be finite")
        return value

    @field_validator("observed_at", "window_start", "window_end", "expires_at")
    @classmethod
    def timestamps_must_include_timezone(cls, value: datetime | None) -> datetime | None:
        return _timezone_aware(value)

    @model_validator(mode="after")
    def apply_metric_rules(self) -> "MetricObservationCreate":
        if self.value is None and not (self.missing_reason or "").strip():
            raise ValueError("missing_reason is required when the value is absent")
        if self.value is not None and self.evidence_id is None:
            raise ValueError("a numeric observation requires an evidence_id")
        if self.value is not None and self.missing_reason is not None:
            raise ValueError("a valued observation must not carry a missing reason")
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("window_start and window_end must both be present or both absent")
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_start > self.window_end
        ):
            raise ValueError("window_start must not follow window_end")
        if self.basis in _WINDOWED_BASES and (
            self.window_start is None or self.window_end is None
        ):
            raise ValueError(f"{self.basis} observations require a metric time window")
        return self


class MetricObservation(MetricObservationCreate):
    id: UUID
    owner_user_id: UUID
    collected_at: datetime
    legacy_backfill: bool = False


class MetricObservationPage(StrictModel):
    items: list[MetricObservation]
    next_cursor: str | None


class ContentReferenceCreate(StrictModel):
    platform: Platform | None = None
    book_id: UUID | None = None
    ref_type: ObjectType
    external_ref: str | None = Field(default=None, max_length=200)
    reference_url: str | None = None
    title: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)
    license_scope: Literal["own_account", "licensed_data_service", "manual_observation"]
    usage_basis: str = Field(min_length=1, max_length=500)
    captured_fields: list[
        Literal["title", "author", "platform_ref", "url", "observed_note", "publication_date", "platform_label"]
    ] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)
    observed_at: datetime | None = None
    material_statement: Literal["no_third_party_media_or_verbatim_transcript"]

    @field_validator("reference_url")
    @classmethod
    def reference_url_must_be_a_web_uri(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_web_uri(value, "reference_url")
        return value

    @field_validator("observed_at")
    @classmethod
    def observed_at_must_include_timezone(cls, value: datetime | None) -> datetime | None:
        return _timezone_aware(value)


class ContentReference(ContentReferenceCreate):
    id: UUID
    owner_user_id: UUID
    created_at: datetime


class ContentReferencePage(StrictModel):
    items: list[ContentReference]
    next_cursor: str | None


StyleCardStatus = Literal["draft", "pending_review", "approved", "rejected"]


class StyleFeature(StrictModel):
    feature_key: Literal[
        "opening_hook",
        "narrative_structure",
        "pacing",
        "visual_style",
        "subtitle_style",
        "tone",
        "cta",
        "audience_match",
        "other",
    ]
    label: str = Field(min_length=1, max_length=100)
    explanation: str = Field(min_length=1, max_length=500)
    evidence_ids: list[UUID] = Field(min_length=1)
    sample_count: int = Field(ge=1)


class StyleCardCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    target_platform: Platform | None = None
    book_id: UUID | None = None
    derived_by: Literal["manual", "workflow_extraction"] = "manual"
    features: list[StyleFeature] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)
    reference_ids: list[UUID] = Field(default_factory=list)
    human_note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def features_reference_evidence(self) -> "StyleCardCreate":
        evidence = set(self.evidence_ids)
        for feature in self.features:
            if not set(feature.evidence_ids) <= evidence:
                raise ValueError("each feature's evidence_ids must be part of the card evidence_ids")
        return self


class StyleCardUpdate(StrictModel):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    target_platform: Platform | None = None
    features: list[StyleFeature] | None = None
    evidence_ids: list[UUID] | None = None
    reference_ids: list[UUID] | None = None
    human_note: str | None = Field(default=None, max_length=1000)


class StyleCardReview(StrictModel):
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def reject_requires_note(self) -> "StyleCardReview":
        if self.decision == "reject" and not (self.note or "").strip():
            raise ValueError("a rejected style card requires a note")
        return self


class StyleCard(StyleCardCreate):
    id: UUID
    owner_user_id: UUID
    created_at: datetime
    updated_at: datetime
    revision: int = Field(ge=1)
    review_status: StyleCardStatus
    edited_by_user_id: UUID | None
    last_edited_at: datetime | None
    abstract_only: bool = True
    evidence_sample_count: int = Field(ge=0)


class StyleCardPage(StrictModel):
    items: list[StyleCard]
    next_cursor: str | None


ImportStatus = Literal["preview", "applied", "partially_applied", "failed"]

ImportRowCode = Literal[
    "encoding_error",
    "malformed_row",
    "missing_required_field",
    "invalid_book_isbn",
    "invalid_number",
    "invalid_timestamp",
    "invalid_platform",
    "invalid_url",
    "formula_injection",
    "duplicate_natural_key",
    "edition_mismatch_suspected",
    "unknown_column",
    "field_too_long",
    "invalid_mapping",
    "reference_rights_unsupported",
]


class ImportRowDiagnostic(StrictModel):
    row_number: int = Field(ge=1)
    field: str | None = Field(default=None, max_length=100)
    code: ImportRowCode
    message: str = Field(max_length=500)
    severity: Literal["error", "warning"]


class ImportPreview(StrictModel):
    id: UUID
    owner_user_id: UUID
    status: ImportStatus
    filename: str = Field(max_length=200)
    encoding_detected: Literal["utf8", "utf8_bom", "unknown"]
    file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    columns: list[str]
    total_rows: int = Field(ge=0)
    parsed_rows: int = Field(ge=0)
    valid_rows: int = Field(ge=0)
    failed_rows: int = Field(ge=0)
    skipped_duplicate_rows: int = Field(ge=0)
    mapping_suggestions: dict[str, str | None]
    row_diagnostics: list[ImportRowDiagnostic]
    created_at: datetime
    expires_at: datetime


class ImportConfirm(StrictModel):
    mapping: dict[str, str | None]
    on_duplicate: Literal["skip", "update"] = "skip"
    match_by: Literal["isbn", "isbn_and_edition", "title_author_fuzzy"] = "isbn_and_edition"
    normalize_isbn: bool = True
    note: str | None = Field(default=None, max_length=500)


class ImportCounts(StrictModel):
    total: int = Field(ge=0)
    parsed: int = Field(ge=0)
    persisted: int = Field(ge=0)
    skipped_duplicate: int = Field(ge=0)
    failed: int = Field(ge=0)
    warnings: int = Field(ge=0)


class ImportJob(StrictModel):
    id: UUID
    owner_user_id: UUID
    status: ImportStatus
    filename: str = Field(max_length=200)
    file_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    updated_at: datetime
    counts: ImportCounts
    row_diagnostics: list[ImportRowDiagnostic]
    entity_kinds: list[
        Literal["book_edition", "promotion_candidate", "metric_observation", "evidence", "content_reference"]
    ]
    mapping: dict[str, str | None]
    on_duplicate: Literal["skip", "update"]
    match_by: Literal["isbn", "isbn_and_edition", "title_author_fuzzy"]


class ImportPage(StrictModel):
    items: list[ImportJob]
    next_cursor: str | None


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
