from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient

from book_video.api.router import create_api_router
from book_video.domain.service import DomainService
from book_video.persistence.database import Database


OWNER_ID = "00000000-0000-4000-8000-000000000001"


def _app(tmp_path, *, submitter=None) -> tuple[TestClient, DomainService, list[dict]]:
    service = DomainService(
        Database(tmp_path / "api-flow.sqlite3"),
        owner_user_id=OWNER_ID,
        render_output_dir=tmp_path / "renders",
    )
    queued: list[dict] = []
    app = FastAPI()
    app.include_router(
        create_api_router(
            service,
            run_submitter=submitter or queued.append,
            render_submitter=queued.append,
        )
    )
    return TestClient(app), service, queued


def _book_body(*, status="human_confirmed") -> dict:
    return {
        "title": "把日子写成地图",
        "edition_label": "虚构演示版 1.0",
        "author": "林例",
        "isbn": None,
        "publisher": None,
        "original_opinion": "虚构演示观点：一次小选择也可以成为地图上的标记。",
        "facts": [
            {
                "key": "title",
                "value": "把日子写成地图",
                "verification_status": status,
                "source_note": "虚构演示数据，仅用于测试",
                "evidence_id": None,
            }
        ],
    }


def _candidate_body(book_id: str, *, confirmed=True) -> dict:
    return {
        "book_id": book_id,
        "platform": "douyin",
        "platform_product_id": "DEMO-NOT-REAL",
        "product_url": "https://example.invalid/demo-product",
        "eligibility_status": "manually_confirmed" if confirmed else "unverified",
        "eligibility_checked_at": datetime.now(timezone.utc).isoformat()
        if confirmed
        else None,
        "eligibility_note": "演示账号后台人工核对，仅供本地测试"
        if confirmed
        else "尚未人工核对",
    }


def _project_body(book_id: str, candidate_id: str) -> dict:
    return {
        "book_id": book_id,
        "promotion_candidate_id": candidate_id,
        "target_audience": "喜欢个人规划的成年读者",
        "target_platform": "douyin",
        "desired_seconds": 30,
        "original_angle": "用原创文字和自制地图图形表达一次小选择。",
        "prohibited_claims": ["真实销量", "疗效", "保证改变人生"],
        "evidence_ids": [],
    }


def _script_body() -> dict:
    return {
        "based_on_version_id": None,
        "segments": [
            {
                "order": 0,
                "onscreen_text": "把一个小选择画在地图上",
                "narration": "这是虚构演示脚本，讨论如何表达一次小选择。",
                "fact_keys": ["title"],
                "duration_seconds": 30,
            }
        ],
        "title_suggestion": "给日常选择画一张小地图",
        "description_suggestion": "虚构演示文案，发布前请人工核对书籍版本和拟挂商品。",
    }


def _approve(client: TestClient, run_id: str, script_id: str, **overrides):
    review = {
        "script_version_id": script_id,
        "decision": "approve",
        "note": "仅测试确认",
        "fact_confirmed": True,
        "product_match_confirmed": True,
        "asset_rights_confirmed": True,
        **overrides,
    }
    return client.post(f"/api/v1/runs/{run_id}/review", json=review)


def _ready_flow(client: TestClient) -> tuple[dict, dict, dict, dict, dict]:
    book_response = client.post("/api/v1/books", json=_book_body())
    assert book_response.status_code == 201, book_response.text
    book = book_response.json()

    candidate_response = client.post(
        "/api/v1/promotion-candidates", json=_candidate_body(book["id"])
    )
    assert candidate_response.status_code == 201, candidate_response.text
    candidate = candidate_response.json()

    project_response = client.post(
        "/api/v1/creative-projects",
        json=_project_body(book["id"], candidate["id"]),
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()

    script_response = client.post(
        f"/api/v1/creative-projects/{project['id']}/scripts", json=_script_body()
    )
    assert script_response.status_code == 201, script_response.text
    script = script_response.json()

    run_response = client.post(
        f"/api/v1/creative-projects/{project['id']}/runs",
        headers={"Idempotency-Key": "flow-run-key-01"},
        json={"brief_version": 1},
    )
    assert run_response.status_code == 202, run_response.text
    run = run_response.json()
    assert run["status"] == "queued"
    duplicate_run_response = client.post(
        f"/api/v1/creative-projects/{project['id']}/runs",
        headers={"Idempotency-Key": "flow-run-key-01"},
        json={"brief_version": 1},
    )
    assert duplicate_run_response.status_code == 202
    assert duplicate_run_response.json()["id"] == run["id"]
    run = service_run_transition_for_test(client, run["id"])
    return book, candidate, project, script, run


def service_run_transition_for_test(client: TestClient, run_id: str) -> dict:
    """Drive the worker-owned transition via an isolated service facade."""
    # The route surface intentionally has no public API for worker-only transitions.
    # This helper attaches the service on the test app, not to user-facing HTTP.
    service = client.app.state.domain_service
    service.transition_run(run_id, status="running", stage="script")
    service.transition_run(run_id, status="waiting_review", stage="review")
    return service.get_run(run_id)


def test_http_flow_requires_human_confirmation_and_returns_manual_package(tmp_path) -> None:
    client, service, queued = _app(tmp_path)
    client.app.state.domain_service = service

    book, candidate, project, script, run = _ready_flow(client)
    assert UUID(book["owner_user_id"]) == UUID(OWNER_ID)
    assert candidate["eligibility_status"] == "manually_confirmed"
    assert run["status"] == "waiting_review"
    assert len(queued) == 1

    approval = _approve(client, run["id"], script["id"])
    assert approval.status_code == 200, approval.text
    assert approval.json()["status"] == "running"
    assert approval.json()["stage"] == "review"
    claimed_review = service.claim_next_reviewed_run()
    assert claimed_review["id"] == run["id"]
    assert service.get_review_request(run["id"])["decision"] == "approve"
    assert service.claim_next_reviewed_run() is None
    service.transition_run(run["id"], status="running", stage="storyboard")

    script_list = client.get(f"/api/v1/creative-projects/{project['id']}/scripts")
    assert script_list.status_code == 200
    assert script_list.json()[0]["review_status"] == "approved"

    render_body = {
        "project_id": project["id"],
        "script_version_id": script["id"],
        "promotion_candidate_id": candidate["id"],
        "width": 1080,
        "height": 1920,
        "asset_rights": [
            {"origin": "self_created_graphic", "usage_status": "allowed"},
            {"origin": "self_created_text", "usage_status": "allowed"},
        ],
    }
    blocked = client.post(
        "/api/v1/renders",
        headers={"Idempotency-Key": "render-key-blocked"},
        json={**render_body, "asset_rights": [{"origin": "licensed", "usage_status": "blocked"}]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "asset_rights_blocked"

    first = client.post(
        "/api/v1/renders",
        headers={"Idempotency-Key": "render-key-0001"},
        json=render_body,
    )
    second = client.post(
        "/api/v1/renders",
        headers={"Idempotency-Key": "render-key-0001"},
        json=render_body,
    )
    assert first.status_code == second.status_code == 202
    render = first.json()
    assert render["id"] == second.json()["id"]
    assert render["status"] == "queued"
    assert len(queued) == 2

    assert service.list_queued_renders()[0]["id"] == render["id"]
    claimed_render = service.claim_next_render()
    assert claimed_render["id"] == render["id"]
    assert claimed_render["status"] == "running"
    assert service.claim_next_render() is None
    render_snapshot = service.get_render_snapshot(render["id"])
    assert render_snapshot["script_version"]["id"] == script["id"]
    assert render_snapshot["asset_rights"][0]["usage_status"] == "allowed"
    not_ready = client.get(
        f"/api/v1/creative-projects/{project['id']}/publication-package"
    )
    assert not_ready.status_code == 404
    assert not_ready.json()["code"] == "publication_package_not_ready"

    output_path = service.render_output_dir / "preview.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"fictional demo render bytes")
    service.complete_render(render["id"], str(output_path))
    package_response = client.get(
        f"/api/v1/creative-projects/{project['id']}/publication-package"
    )
    assert package_response.status_code == 200, package_response.text
    package = package_response.json()
    assert package["publication_state"] == "needs_manual_checks"
    assert package["render_id"] == render["id"]
    assert package["checklist"] == {
        "account_eligible": False,
        "product_eligible": True,
        "edition_matches": True,
        "rights_confirmed": True,
        "manual_attachment_required": True,
    }
    assert package["video_download_url"] == (
        f"http://testserver/api/v1/renders/{render['id']}/download"
    )
    download = client.get(package["video_download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "video/mp4"
    assert download.content == b"fictional demo render bytes"
    other_owner = DomainService(
        service.database,
        owner_user_id="00000000-0000-4000-8000-000000000002",
        render_output_dir=service.render_output_dir,
    )
    try:
        other_owner.get_render_download_path(render["id"])
    except Exception as error:
        assert getattr(error, "status_code", None) == 404
    else:
        raise AssertionError("another owner unexpectedly downloaded the render")
    saved_package = service.save_publication_package(project["id"], render["id"])
    assert saved_package["id"] == package["id"]

    events_response = client.get(
        f"/api/v1/runs/{run['id']}/events", headers={"Last-Event-ID": "1"}
    )
    assert events_response.status_code == 200
    assert events_response.headers["content-type"].startswith("text/event-stream")
    assert "id: 2\nevent: run.stage_changed" in events_response.text
    events = service.list_run_events(run["id"])
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))


def test_review_rejects_unverified_facts_and_unconfirmed_product(tmp_path) -> None:
    client, service, _ = _app(tmp_path)
    client.app.state.domain_service = service

    book_response = client.post("/api/v1/books", json=_book_body(status="unverified"))
    candidate_response = client.post(
        "/api/v1/promotion-candidates",
        json=_candidate_body(book_response.json()["id"], confirmed=False),
    )
    project_response = client.post(
        "/api/v1/creative-projects",
        json=_project_body(book_response.json()["id"], candidate_response.json()["id"]),
    )
    project_id = project_response.json()["id"]
    script = client.post(
        f"/api/v1/creative-projects/{project_id}/scripts", json=_script_body()
    ).json()
    run = client.post(
        f"/api/v1/creative-projects/{project_id}/runs",
        headers={"Idempotency-Key": "review-run-key"},
        json={"brief_version": 1},
    ).json()
    service.transition_run(run["id"], status="running", stage="script")
    service.transition_run(run["id"], status="waiting_review", stage="review")

    response = _approve(client, run["id"], script["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "book_fact_unverified"
    assert set(response.json()) >= {"code", "message", "request_id"}


def test_review_rejects_unconfirmed_product_eligibility(tmp_path) -> None:
    client, service, _ = _app(tmp_path)
    client.app.state.domain_service = service
    book = client.post("/api/v1/books", json=_book_body()).json()
    candidate = client.post(
        "/api/v1/promotion-candidates",
        json=_candidate_body(book["id"], confirmed=False),
    ).json()
    project = client.post(
        "/api/v1/creative-projects",
        json=_project_body(book["id"], candidate["id"]),
    ).json()
    script = client.post(
        f"/api/v1/creative-projects/{project['id']}/scripts", json=_script_body()
    ).json()
    run = client.post(
        f"/api/v1/creative-projects/{project['id']}/runs",
        headers={"Idempotency-Key": "eligibility-run-key"},
        json={"brief_version": 1},
    ).json()
    service.transition_run(run["id"], status="running", stage="script")
    service.transition_run(run["id"], status="waiting_review", stage="review")

    response = _approve(client, run["id"], script["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "product_not_confirmed"


def test_owner_is_assigned_by_server_and_records_are_owner_scoped(tmp_path) -> None:
    client, service, _ = _app(tmp_path)
    client.app.state.domain_service = service

    body = _book_body()
    response = client.post("/api/v1/books", json=body)
    assert response.status_code == 201
    book = response.json()
    assert book["owner_user_id"] == OWNER_ID

    forged = client.post("/api/v1/books", json={**body, "owner_user_id": OWNER_ID})
    assert forged.status_code == 422
    assert forged.json()["code"] == "validation_error"

    other_owner_service = DomainService(
        service.database, owner_user_id="00000000-0000-4000-8000-000000000002"
    )
    try:
        other_owner_service.get_book(book["id"])
    except Exception as error:
        assert getattr(error, "status_code", None) == 404
    else:
        raise AssertionError("other owner unexpectedly read the book")


def test_manual_confirmation_needs_platform_check_evidence(tmp_path) -> None:
    client, _, _ = _app(tmp_path)
    book = client.post("/api/v1/books", json=_book_body()).json()
    candidate = _candidate_body(book["id"])
    candidate["eligibility_checked_at"] = None
    candidate["eligibility_note"] = None
    response = client.post("/api/v1/promotion-candidates", json=candidate)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_failed_approval_confirmation_does_not_advance_run(tmp_path) -> None:
    client, service, _ = _app(tmp_path)
    client.app.state.domain_service = service
    _, _, _, script, run = _ready_flow(client)

    response = _approve(client, run["id"], script["id"], asset_rights_confirmed=False)
    assert response.status_code == 409
    assert response.json()["details"]["field"] == "asset_rights_confirmed"
    assert service.get_run(run["id"])["status"] == "waiting_review"


def test_worker_entry_points_claim_queue_and_replay_run_events_idempotently(tmp_path) -> None:
    client, service, _ = _app(tmp_path)
    client.app.state.domain_service = service
    book = client.post("/api/v1/books", json=_book_body()).json()
    candidate = client.post(
        "/api/v1/promotion-candidates", json=_candidate_body(book["id"])
    ).json()
    project = client.post(
        "/api/v1/creative-projects",
        json=_project_body(book["id"], candidate["id"]),
    ).json()
    run = client.post(
        f"/api/v1/creative-projects/{project['id']}/runs",
        headers={"Idempotency-Key": "worker-run-key"},
        json={"brief_version": 1},
    ).json()

    queued_runs = service.list_queued_runs()
    assert len(queued_runs) == 1
    assert queued_runs[0]["id"] == run["id"]
    claimed = service.claim_next_run()
    assert claimed["id"] == run["id"]
    assert claimed["status"] == "running"
    assert claimed["sequence"] == 2
    run_snapshot = service.get_run_snapshot(run["id"])
    assert run_snapshot["brief_version"] == 1
    assert run_snapshot["snapshot"]["project"]["id"] == project["id"]
    assert service.claim_next_run() is None
    assert service.list_queued_runs() == []
    snapshot = service.get_generation_snapshot(project["id"])
    assert snapshot["project"]["id"] == project["id"]
    assert snapshot["book"]["id"] == book["id"]
    assert snapshot["promotion_candidate"]["id"] == candidate["id"]

    event = {
        "event_id": "00000000-0000-4000-8000-000000000251",
        "run_id": run["id"],
        "project_id": project["id"],
        "sequence": 3,
        "type": "run.stage_changed",
        "status": "running",
        "stage": "script",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "script_version_id": None,
        "render_id": None,
        "reason_code": None,
        "message": "worker started script generation",
    }
    assert service.persist_run_event(event) == service.persist_run_event(event)
    assert service.get_run(run["id"])["sequence"] == 3
    assert [item["sequence"] for item in service.list_run_events(run["id"])] == [1, 2, 3]

    review_requested = {
        **event,
        "event_id": "00000000-0000-4000-8000-000000000252",
        "sequence": 4,
        "type": "run.review_requested",
        "status": "waiting_review",
        "stage": "review",
        "message": "waiting for manual review",
    }
    stored_request = service.persist_run_event(review_requested)
    replayed_request = service.persist_run_event(
        {
            **review_requested,
            "occurred_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }
    )
    assert replayed_request == stored_request
    assert service.get_run(run["id"])["sequence"] == 4


def test_generated_script_persistence_keeps_stable_identity_and_review_state(tmp_path) -> None:
    client, service, _ = _app(tmp_path)
    book = client.post("/api/v1/books", json=_book_body()).json()
    candidate = client.post(
        "/api/v1/promotion-candidates", json=_candidate_body(book["id"])
    ).json()
    project = client.post(
        "/api/v1/creative-projects",
        json=_project_body(book["id"], candidate["id"]),
    ).json()
    script = {
        **_script_body(),
        "id": "00000000-0000-4000-8000-000000000261",
        "project_id": project["id"],
        "owner_user_id": OWNER_ID,
        "version_number": 4,
        "review_status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    first = service.persist_generated_script_version(project["id"], script)
    replay = service.persist_generated_script_version(project["id"], script)
    assert first == replay
    assert first["id"] == script["id"]
    assert first["version_number"] == 4
    assert first["review_status"] == "pending"

    forged_approval = {**script, "review_status": "approved"}
    try:
        service.persist_generated_script_version(project["id"], forged_approval)
    except Exception as error:
        assert getattr(error, "code", None) == "script_review_not_persisted"
    else:
        raise AssertionError("generated worker bypassed the persisted human review")


def test_sqlite_migration_survives_database_reopen(tmp_path) -> None:
    path = tmp_path / "persistent.sqlite3"
    first = DomainService(Database(path), owner_user_id=OWNER_ID)
    book = first.create_book(_book_body())
    first.database.close()

    reopened = DomainService(Database(path), owner_user_id=OWNER_ID)
    assert reopened.get_book(book["id"]) == book
    with reopened.database.read() as conn:
        migrations = conn.execute("SELECT version FROM schema_migrations").fetchall()
    assert [row[0] for row in migrations] == ["001_initial"]


def test_default_database_path_is_local_and_environment_override_creates_parent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    database = Database()
    assert Path(database.path) == tmp_path / ".local" / "book_video.sqlite3"
    assert Path(database.path).is_file()
    database.close()

    override_path = tmp_path / "configured" / "custom.sqlite3"
    monkeypatch.setenv("BOOK_VIDEO_DATABASE_PATH", str(override_path))
    overridden = Database()
    assert Path(overridden.path) == override_path
    assert override_path.is_file()
    overridden.close()


def test_render_rejects_output_outside_server_artifact_root(tmp_path) -> None:
    client, service, _ = _app(tmp_path)
    client.app.state.domain_service = service
    _, candidate, project, script, run = _ready_flow(client)
    assert _approve(client, run["id"], script["id"]).status_code == 200
    render = client.post(
        "/api/v1/renders",
        headers={"Idempotency-Key": "outside-path-render"},
        json={
            "project_id": project["id"],
            "script_version_id": script["id"],
            "promotion_candidate_id": candidate["id"],
            "width": 1080,
            "height": 1920,
            "asset_rights": [
                {"origin": "self_created_graphic", "usage_status": "allowed"},
                {"origin": "self_created_text", "usage_status": "allowed"},
            ],
        },
    ).json()
    outside_path = tmp_path / "outside.mp4"
    outside_path.write_bytes(b"not a server-produced artifact")

    try:
        service.complete_render(render["id"], str(outside_path))
    except Exception as error:
        assert getattr(error, "code", None) == "render_output_outside_root"
    else:
        raise AssertionError("render output outside the artifact root was accepted")
    assert service.get_render(render["id"])["status"] == "queued"
