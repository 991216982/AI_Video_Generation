"""Validate frozen stage 0 contracts and harmless sample data."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, RefResolver


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "packages" / "contracts"


def main() -> None:
    spec = yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))
    assert spec["openapi"] == "3.1.0"
    assert len(spec["paths"]) >= 10
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
    print("stage 0 contracts and examples: OK")


if __name__ == "__main__":
    main()
