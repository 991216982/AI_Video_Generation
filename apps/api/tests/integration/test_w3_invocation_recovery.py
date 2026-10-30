"""W3 recovery checks use only fictional book data and a counting fake provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from book_video.api.router import create_api_router
from book_video.domain.service import DomainService
from book_video.integration import SELF_CREATED_RIGHTS, _generation_adapter
from book_video.persistence.database import Database
from book_video.workflows.checkpoint import SQLiteCheckpointSaver
from book_video.workflows.model_adapter import ModelResponse
from book_video.workflows.script_generation import (
    ManualReconciliationRequired,
    _make_scripts,
    create_script_generation_graph,
    run_script_generation,
)


ROOT = Path(__file__).resolve().parents[4]


def _started_run(path: Path):
    service = DomainService(Database(path), render_output_dir=path.parent / "renders")
    app = FastAPI()
    app.include_router(create_api_router(service))
    client = TestClient(app)
    sample = json.loads((ROOT / "packages/contracts/examples/manual-book.json").read_text(encoding="utf-8"))
    assert sample["sample_only"] is True
    book = client.post("/api/v1/books", json=sample["book"]).json()
    candidate = client.post("/api/v1/promotion-candidates", json={
        **sample["promotion_candidate"], "book_id": book["id"],
    }).json()
    project = client.post("/api/v1/creative-projects", json={
        **sample["project_brief"], "book_id": book["id"],
        "promotion_candidate_id": candidate["id"], "evidence_ids": [],
    }).json()
    run = client.post(f"/api/v1/creative-projects/{project['id']}/runs",
                      headers={"Idempotency-Key": "w3-fault-injection-run"},
                      json={"brief_version": 1}).json()
    claimed = service.claim_next_run()
    assert claimed is not None and claimed["id"] == run["id"]
    snapshot = dict(service.get_run_snapshot(run["id"])["snapshot"])
    snapshot["run"] = claimed
    snapshot["asset_rights"] = SELF_CREATED_RIGHTS
    return service, run["id"], project["id"], snapshot


class CountingProvider:
    def __init__(self, scripts):
        self.scripts = scripts
        self.calls: list[str] = []
        self.fail_after_call = False

    def generate(self, *, variant, context):
        self.calls.append(context["invocation_key"])
        if self.fail_after_call:
            raise RuntimeError("simulated process crash after sending provider request")
        return ModelResponse(
            script=self.scripts[variant], source="external_model",
            provider_key="fictional_provider", model_key="fictional_model",
            amount_minor=37, missing_reason=None, input_tokens=12, output_tokens=8,
        )


def test_crash_after_result_and_cost_before_script_save_reuses_provider_output(tmp_path: Path):
    path = tmp_path / "recovery.sqlite3"
    service, run_id, project_id, snapshot = _started_run(path)
    fixtures = dict(zip(("A", "B"), _make_scripts(snapshot, run_id)))
    provider = CountingProvider(fixtures)
    saver = SQLiteCheckpointSaver(path)
    first_save = True

    def crash_before_script_save(script):
        nonlocal first_save
        if first_save:
            first_save = False
            raise RuntimeError("simulated crash before script save")
        service.persist_generated_script_version(project_id, script)

    with pytest.raises(RuntimeError, match="before script save"):
        run_script_generation(snapshot, run_id=run_id, checkpointer=saver,
                              persist_generation=_generation_adapter(service),
                              persist_script=crash_before_script_save,
                              model_adapter=provider)
    assert provider.calls == [f"{run_id}:script:A:external_model"]
    first_result = service.list_generation_results(run_id)[0]
    first_cost = service.list_run_costs(run_id)[0]
    assert first_cost["amount_minor"] == 37
    assert service.list_script_versions(project_id) == []
    service.database.close()

    reopened = DomainService(Database(path), render_output_dir=tmp_path / "renders")
    graph = create_script_generation_graph(
        checkpointer=SQLiteCheckpointSaver(path),
        persist_generation=_generation_adapter(reopened), model_adapter=provider,
        persist_script=lambda script: reopened.persist_generated_script_version(project_id, script),
    )
    graph.invoke(None, config={"configurable": {"thread_id": run_id}})
    assert provider.calls == [f"{run_id}:script:A:external_model", f"{run_id}:script:B:external_model"]
    results = reopened.list_generation_results(run_id)
    costs = reopened.list_run_costs(run_id)
    scripts = reopened.list_script_versions(project_id)
    assert len(results) == len(costs) == len(scripts) == 2
    assert results[0]["id"] == first_result["id"]
    assert costs[0]["id"] == first_cost["id"]
    assert {cost["generation_result_id"] for cost in costs} == {result["id"] for result in results}
    assert all(cost["amount_minor"] == 37 for cost in costs)
    assert {script["generation_result_id"] for script in scripts} == {result["id"] for result in results}
    reopened.database.close()


def test_unknown_inflight_provider_call_requires_manual_reconciliation(tmp_path: Path):
    path = tmp_path / "uncertain.sqlite3"
    service, run_id, _project_id, snapshot = _started_run(path)
    fixtures = dict(zip(("A", "B"), _make_scripts(snapshot, run_id)))
    provider = CountingProvider(fixtures)
    provider.fail_after_call = True
    with pytest.raises(RuntimeError, match="after sending"):
        run_script_generation(snapshot, run_id=run_id, checkpointer=SQLiteCheckpointSaver(path),
                              persist_generation=_generation_adapter(service), model_adapter=provider)
    service.database.close()

    reopened = DomainService(Database(path), render_output_dir=tmp_path / "renders")
    graph = create_script_generation_graph(checkpointer=SQLiteCheckpointSaver(path),
                                           persist_generation=_generation_adapter(reopened),
                                           model_adapter=provider)
    with pytest.raises(ManualReconciliationRequired):
        graph.invoke(None, config={"configurable": {"thread_id": run_id}})
    assert provider.calls == [f"{run_id}:script:A:external_model"]
    results = reopened.list_generation_results(run_id)
    costs = reopened.list_run_costs(run_id)
    assert len(results) == len(costs) == 1
    assert results[0]["reason_code"] == "manual_reconciliation_required"
    assert costs[0]["generation_result_id"] == results[0]["id"]
    assert costs[0]["amount_minor"] is None
    assert costs[0]["missing_reason"] == "provider_billing_unavailable"
    reopened.database.close()


def test_provider_result_lookup_recovers_uncertain_call_without_resending(tmp_path: Path):
    path = tmp_path / "queryable.sqlite3"
    service, run_id, project_id, snapshot = _started_run(path)
    fixtures = dict(zip(("A", "B"), _make_scripts(snapshot, run_id)))

    class QueryableProvider(CountingProvider):
        def __init__(self):
            super().__init__(fixtures)
            self.responses = {}
            self.crash_once = True

        def generate(self, *, variant, context):
            response = super().generate(variant=variant, context=context)
            self.responses[context["invocation_key"]] = response
            if self.crash_once:
                self.crash_once = False
                raise RuntimeError("simulated crash after provider completed")
            return response

        def lookup_result(self, invocation_key):
            return self.responses.get(invocation_key)

    provider = QueryableProvider()
    with pytest.raises(RuntimeError, match="provider completed"):
        run_script_generation(snapshot, run_id=run_id, checkpointer=SQLiteCheckpointSaver(path),
                              persist_generation=_generation_adapter(service), model_adapter=provider)
    service.database.close()

    reopened = DomainService(Database(path), render_output_dir=tmp_path / "renders")
    graph = create_script_generation_graph(
        checkpointer=SQLiteCheckpointSaver(path),
        persist_generation=_generation_adapter(reopened), model_adapter=provider,
        persist_script=lambda script: reopened.persist_generated_script_version(project_id, script),
    )
    graph.invoke(None, config={"configurable": {"thread_id": run_id}})
    assert provider.calls == [f"{run_id}:script:A:external_model", f"{run_id}:script:B:external_model"]
    assert len(reopened.list_generation_results(run_id)) == 2
    assert [cost["amount_minor"] for cost in reopened.list_run_costs(run_id)] == [37, 37]
    assert len(reopened.list_script_versions(project_id)) == 2
    reopened.database.close()
