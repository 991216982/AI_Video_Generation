"""W3 checks use invented book facts and no provider or platform account."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from langgraph.checkpoint.memory import MemorySaver

from book_video.workflows.model_adapter import ModelAdapterError, ModelResponse
from book_video.workflows.script_generation import ScriptGenerationError, run_script_generation

from test_script_generation import RUN_ID, generation_snapshot


def test_two_local_variants_record_once_and_pin_exact_fact_revision(tmp_path: Path) -> None:
    recorded: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    path = tmp_path / "checkpoint.sqlite3"
    from book_video.workflows.checkpoint import SQLiteCheckpointSaver

    def persist_generation(result: dict[str, Any], cost: dict[str, Any]) -> dict[str, Any]:
        key = result["invocation_key"]
        if key not in recorded:
            result_id = str(uuid5(NAMESPACE_URL, key))
            recorded[key] = {
                "generation_result": {**result, "id": result_id},
                "cost_record": {**cost, "id": str(uuid5(NAMESPACE_URL, key + ":cost")), "generation_result_id": result_id},
            }
        return recorded[key]

    first = run_script_generation(
        generation_snapshot(), run_id=RUN_ID,
        checkpointer=SQLiteCheckpointSaver(path),
        persist_generation=persist_generation, emit_event=events.append,
    )
    replay = run_script_generation(
        generation_snapshot(), run_id=RUN_ID,
        checkpointer=SQLiteCheckpointSaver(path),
        persist_generation=persist_generation, emit_event=events.append,
    )
    assert first["status"] == replay["status"] == "waiting_review"
    assert len(recorded) == 2
    assert all(script["generation_result_id"] for script in first["scripts"])
    assert all(script["fact_revision_refs"] == [{"key": "title", "revision": 1}] for script in first["scripts"])
    assert all(result["source"] == "deterministic_fixture" for result in first["generation_results"])
    assert all(cost["amount_minor"] == 0 for cost in first["cost_records"])
    assert len([event for event in events if event["type"] == "run.cost_recorded"]) == 2
    assert all(event["attempt"] == 1 and event["generation_result_id"] and event["cost_record_id"] for event in events if event["type"] == "run.cost_recorded")


def test_external_invalid_fact_and_prohibited_text_fall_back_with_reason_and_unknown_cost() -> None:
    class BadAdapter:
        def generate(self, *, variant: str, context: dict[str, Any]) -> ModelResponse:
            assert all(fact["key"] != "unverified_note" for fact in context["verified_facts"])
            assert "fixture_scripts" not in context
            assert context["invocation_key"] == f"{RUN_ID}:script:{variant}:external_model"
            segment = {"order": 0, "onscreen_text": "真实销量", "narration": "真实销量", "fact_keys": ["unverified_note"], "duration_seconds": 30.0}
            return ModelResponse(
                script={"title_suggestion": "外部草稿", "description_suggestion": "演示", "segments": [segment]},
                source="external_model", provider_key="test_provider", model_key="test_model",
            )

    snapshot = generation_snapshot()
    paused = run_script_generation(snapshot, run_id=RUN_ID, checkpointer=MemorySaver(), model_adapter=BadAdapter())
    assert paused["status"] == "waiting_review"
    failed = [result for result in paused["generation_results"] if result["status"] == "failed"]
    assert len(failed) == 2
    assert all(result["reason_code"] == "prohibited_claim" for result in failed)
    assert all(result["script_version_id"] is None for result in failed)
    assert all(cost["amount_minor"] is None and cost["missing_reason"] for cost in paused["cost_records"] if cost["generation_result_id"] in {result["id"] for result in failed})
    assert len([result for result in paused["generation_results"] if result["source"] == "deterministic_fixture"]) == 2


def test_provider_error_falls_back_and_unbased_fact_is_rejected() -> None:
    class TimedOut:
        def generate(self, *, variant: str, context: dict[str, Any]) -> ModelResponse:
            raise ModelAdapterError("provider_timeout", "invented timeout")

    paused = run_script_generation(generation_snapshot(), run_id=RUN_ID, checkpointer=MemorySaver(), model_adapter=TimedOut())
    assert len([result for result in paused["generation_results"] if result["reason_code"] == "provider_timeout"]) == 2

    unbased = deepcopy(generation_snapshot())
    unbased["book"]["facts"][0]["source_note"] = None
    with pytest.raises(ScriptGenerationError, match="verified bibliographic fact"):
        run_script_generation(unbased, run_id=RUN_ID, checkpointer=MemorySaver())
