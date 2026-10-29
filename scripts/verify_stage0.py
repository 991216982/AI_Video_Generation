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
    assert spec["info"]["version"] == "0.3.0"
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
    print("stage 0 contracts and examples: OK")


if __name__ == "__main__":
    main()
