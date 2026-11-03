"""Validate frozen stage 0 contracts and harmless sample data."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, RefResolver


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "packages" / "contracts"


def main() -> None:
    spec = yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))
    assert spec["openapi"] == "3.1.0"
    assert spec["info"]["version"] == "0.4.0"
    assert len(spec["paths"]) >= 10
    operation_ids = [
        operation["operationId"]
        for methods in spec["paths"].values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]
    assert len(operation_ids) == len(set(operation_ids))
    schemas = spec["components"]["schemas"]
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)

    # Reject unresolved local references before agents build against this draft.
    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$ref":
                    assert isinstance(child, str) and child.startswith("#/"), child
                    node: object = spec
                    for part in child[2:].split("/"):
                        node = node[part]  # type: ignore[index]
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(spec)
    resolver = RefResolver.from_schema(spec)
    sample = json.loads((CONTRACTS / "examples" / "manual-book.json").read_text(encoding="utf-8"))
    assert sample["sample_only"] is True
    for sample_key, schema_name in (
        ("book", "BookCreate"),
        ("promotion_candidate", "PromotionCandidateCreate"),
        ("evidence", "EvidenceCreate"),
        ("project_brief", "CreativeProjectCreate"),
    ):
        # IDs are supplied when records are created, so validate the portable fields here.
        obj = sample[sample_key].copy()
        if sample_key == "promotion_candidate":
            obj["book_id"] = "00000000-0000-4000-8000-000000000003"
        if sample_key == "project_brief":
            obj["book_id"] = "00000000-0000-4000-8000-000000000003"
            obj["promotion_candidate_id"] = "00000000-0000-4000-8000-000000000004"
        Draft202012Validator(schemas[schema_name], resolver=resolver).validate(obj)

    event_schema = json.loads((CONTRACTS / "events.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(event_schema)
    event = json.loads((CONTRACTS / "examples" / "run-event.json").read_text(encoding="utf-8"))
    Draft202012Validator(event_schema).validate(event)
    assert event["sequence"] == 1
    package = json.loads((CONTRACTS / "examples" / "publication-package.json").read_text(encoding="utf-8"))
    Draft202012Validator(schemas["PublicationPackage"], resolver=resolver).validate(package)
    w2 = json.loads((CONTRACTS / "examples" / "w2-workspace.json").read_text(encoding="utf-8"))
    assert w2["sample_only"] is True
    for sample_key, schema_name in (
        ("book_page", "BookPage"),
        ("project_page", "CreativeProjectPage"),
        ("workspace", "CreativeWorkspace"),
        ("eligibility_update", "PromotionEligibilityUpdate"),
        ("cancel_request", "CancelRequest"),
        ("render_job", "RenderJob"),
        ("error", "ApiError"),
    ):
        Draft202012Validator(schemas[schema_name], resolver=resolver).validate(w2[sample_key])
    invalid_confirmation = {**w2["eligibility_update"], "confirmation_basis": None}
    assert not Draft202012Validator(schemas["PromotionEligibilityUpdate"], resolver=resolver).is_valid(invalid_confirmation)
    for event_name in ("run-cancel-requested.json", "run-retry-requested.json"):
        candidate = json.loads((CONTRACTS / "examples" / event_name).read_text(encoding="utf-8"))
        Draft202012Validator(event_schema).validate(candidate)
    g3 = json.loads((CONTRACTS / "examples" / "g3-contract.json").read_text(encoding="utf-8"))
    assert g3["sample_only"] is True
    for sample_key, schema_name in (
        ("fact_revision_request", "BookFactRevisionRequest"),
        ("fact_revision", "BookFactRevision"),
        ("audio_asset", "AudioAsset"),
        ("render_request", "RenderCreate"),
        ("generation_result", "GenerationResult"),
        ("cost_record", "ModelCostRecord"),
    ):
        Draft202012Validator(schemas[schema_name], resolver=resolver).validate(g3[sample_key])
    Draft202012Validator(event_schema).validate(g3["event"])
    assert not Draft202012Validator(event_schema).is_valid(
        {key: value for key, value in g3["event"].items() if key != "cost_record_id"}
    )
    candidate_create = sample["promotion_candidate"]
    candidate_create = {**candidate_create, "book_id": "00000000-0000-4000-8000-000000000003"}
    create_validator = Draft202012Validator(schemas["PromotionCandidateCreate"], resolver=resolver)
    assert create_validator.is_valid(candidate_create)
    assert not create_validator.is_valid({**candidate_create, "eligibility_status": "manually_confirmed"})
    assert not create_validator.is_valid({**candidate_create, "confirmation_basis": "creator_account_backend"})
    assert not create_validator.is_valid({**candidate_create, "eligibility_checked_at": "2026-10-29T00:00:00Z"})
    confirmed_response = {
        **w2["workspace"]["promotion_candidate"],
        "eligibility_status": "manually_confirmed",
        "eligibility_checked_at": "2026-10-29T00:00:00Z",
        "eligibility_checked_by_user_id": "00000000-0000-4000-8000-000000000099",
    }
    Draft202012Validator(schemas["PromotionCandidate"], resolver=resolver).validate(confirmed_response)
    assert not Draft202012Validator(schemas["BookFactRevisionRequest"], resolver=resolver).is_valid(
        {**g3["fact_revision_request"], "source_note": None}
    )
    assert not Draft202012Validator(schemas["RenderCreate"], resolver=resolver, format_checker=FormatChecker()).is_valid(
        {**g3["render_request"], "audio_asset_id": "C:\\secret\\voice.wav"}
    )

    # --- G4 v0.4 contract increment: research desk, metric evidence, references, style cards, CSV import ---
    g4 = json.loads((CONTRACTS / "examples" / "g4-research.json").read_text(encoding="utf-8"))
    assert g4["sample_only"] is True
    for sample_key, schema_name in (
        ("evidence_manual", "EvidenceCreate"),
        ("evidence_order", "EvidenceCreate"),
        ("evidence_page", "EvidenceCreate"),
        ("metric_order_sales", "MetricObservation"),
        ("metric_platform_display", "MetricObservation"),
        ("metric_estimate", "MetricObservation"),
        ("metric_engagement", "MetricObservation"),
        ("metric_missing", "MetricObservation"),
        ("content_reference_own", "ContentReference"),
        ("content_reference_manual", "ContentReference"),
        ("style_card", "StyleCard"),
        ("style_card_draft_unevidenced", "StyleCard"),
        ("import_preview", "ImportPreview"),
        ("import_confirm", "ImportConfirm"),
        ("import_job", "ImportJob"),
    ):
        Draft202012Validator(schemas[schema_name], resolver=resolver).validate(g4[sample_key])

    metric_schema = schemas["MetricObservationCreate"]
    metric_validator = Draft202012Validator(metric_schema, resolver=resolver)
    metric_response_only = {"id", "owner_user_id", "collected_at", "legacy_backfill"}

    def metric_create(observation: dict) -> dict:
        return {k: v for k, v in observation.items() if k not in metric_response_only}

    # A numeric observation without source evidence must be rejected.
    with_evidence = metric_create(g4["metric_order_sales"])
    missing_evidence = {**with_evidence, "evidence_id": None}
    assert not metric_validator.is_valid(missing_evidence)
    # A null value without a missing reason must be rejected; 0 is never used to mean unknown.
    null_no_reason = {**metric_create(g4["metric_missing"]), "value": None, "missing_reason": None}
    assert not metric_validator.is_valid(null_no_reason)
    null_with_reason = metric_create(g4["metric_missing"])
    assert metric_validator.is_valid(null_with_reason)
    # A present window start requires a matching window end at schema level;
    # ordering (start <= end) and timezone parsing are enforced by the domain/API layer, like v0.3 Evidence.
    partial_window = {**with_evidence, "window_start": "2026-10-25T00:00:00Z", "window_end": None}
    assert not metric_validator.is_valid(partial_window)
    # Only authorized_order_sales may be presented as actual sales; estimates and engagement are separate bases.
    assert g4["metric_estimate"]["basis"] == "third_party_or_manual_estimate"
    assert g4["metric_engagement"]["basis"] == "engagement_signal"
    assert g4["metric_platform_display"]["basis"] == "platform_listing_display"

    ref_schema = schemas["ContentReferenceCreate"]
    ref_validator = Draft202012Validator(ref_schema, resolver=resolver)
    ref_response_only = {"id", "owner_user_id", "created_at"}

    def ref_create(reference: dict) -> dict:
        return {k: v for k, v in reference.items() if k not in ref_response_only}

    # The material declaration is mandatory: no third-party media or verbatim transcript is stored.
    assert not ref_validator.is_valid({**ref_create(g4["content_reference_own"]), "material_statement": None})
    # URL scheme (http/https only; no file paths or client media bytes) and right-scope checks are
    # enforced by the domain/API layer, matching the existing Evidence.source_url and
    # RenderCreate.audio_asset_id rules; the schema itself only declares the uri format.

    card_schema = schemas["StyleCardCreate"]
    card_validator = Draft202012Validator(card_schema, resolver=resolver)
    card_response_only = {
        "id", "owner_user_id", "created_at", "updated_at", "revision", "review_status",
        "edited_by_user_id", "last_edited_at", "abstract_only", "evidence_sample_count",
    }

    def card_create(card: dict) -> dict:
        return {k: v for k, v in card.items() if k not in card_response_only}

    # An evidence-backed card must list its evidence; every feature needs evidence and a sample count.
    feature = g4["style_card"]["features"][0]
    assert not card_validator.is_valid(
        {**card_create(g4["style_card"]), "evidence_ids": [], "features": [{**feature, "evidence_ids": []}]}
    )
    assert not card_validator.is_valid(
        {**card_create(g4["style_card"]), "features": [{**feature, "sample_count": 0}]}
    )
    # An evidence-free draft is allowed only as a provisional draft; it cannot be approved.
    assert card_validator.is_valid(card_create(g4["style_card_draft_unevidenced"]))
    assert g4["style_card_draft_unevidenced"]["evidence_sample_count"] == 0
    assert g4["style_card_draft_unevidenced"]["review_status"] in {"draft", "pending_review"}

    # CSV fixture: UTF-8 BOM, header aligned with the preview sample, six data rows.
    csv_bytes = (CONTRACTS / "examples" / "research-import-fixture.csv").read_bytes()
    assert csv_bytes.startswith(b"\xef\xbb\xbf"), "fixture must carry a UTF-8 BOM sample"
    csv_text = csv_bytes.decode("utf-8-sig")
    csv_lines = [line for line in csv_text.splitlines() if line.strip()]
    header = csv_lines[0].split(",")
    assert header == g4["import_preview"]["columns"]
    assert len(csv_lines) - 1 == g4["import_preview"]["total_rows"]

    # G4 decision: no new run-scoped SSE event this round. CSV import is synchronous request/response
    # with row-level results in the body; research/style endpoints are human-driven writes outside the
    # run lifecycle. The v0.3 event type set below must remain frozen unless W4 integration finds a
    # real async progress need, at which point the contract change goes through the integration owner.
    event_schema = json.loads((CONTRACTS / "events.schema.json").read_text(encoding="utf-8"))
    expected_event_types = {
        "run.created", "run.stage_changed", "run.review_requested", "run.reviewed", "run.failed",
        "run.completed", "run.cancel_requested", "run.cancelled", "run.retry_requested",
        "run.generation_completed", "run.generation_failed", "run.cost_recorded",
    }
    assert set(event_schema["properties"]["type"]["enum"]) == expected_event_types
    print("stage 0 contracts and examples: OK")


if __name__ == "__main__":
    main()
