"""W2 acceptance: reopen the workspace and resume after a worker restart."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from book_video.api.router import create_api_router
from book_video.domain.service import DomainService
from book_video.integration import SELF_CREATED_RIGHTS, process_one
from book_video.media.ffmpeg import probe_mp4
from book_video.persistence.database import Database


ROOT = Path(__file__).resolve().parents[4]
SAMPLE = ROOT / "packages" / "contracts" / "examples" / "manual-book.json"


def _client(db_path: Path, render_dir: Path) -> tuple[DomainService, TestClient]:
    service = DomainService(Database(db_path), render_output_dir=render_dir)
    app = FastAPI()
    app.include_router(create_api_router(service))
    return service, TestClient(app)


def _worker_once(db_path: Path, render_dir: Path) -> None:
    env = os.environ.copy()
    env["BOOK_VIDEO_DATABASE_PATH"] = str(db_path)
    env["BOOK_VIDEO_OUTPUT_DIR"] = str(render_dir)
    result = subprocess.run(
        [sys.executable, "-m", "book_video.worker", "--once"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _seed(client: TestClient) -> tuple[dict, dict, dict]:
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    assert sample["sample_only"] is True
    book_response = client.post("/api/v1/books", json=sample["book"])
    assert book_response.status_code == 201, book_response.text
    book = book_response.json()
    candidate_response = client.post(
        "/api/v1/promotion-candidates",
        json={**sample["promotion_candidate"], "book_id": book["id"]},
    )
    assert candidate_response.status_code == 201, candidate_response.text
    candidate = candidate_response.json()
    evidence_response = client.post("/api/v1/evidence", json=sample["evidence"])
    assert evidence_response.status_code == 201, evidence_response.text
    project_response = client.post(
        "/api/v1/creative-projects",
        json={
            **sample["project_brief"],
            "book_id": book["id"],
            "promotion_candidate_id": candidate["id"],
            "evidence_ids": [evidence_response.json()["id"]],
        },
    )
    assert project_response.status_code == 201, project_response.text
    return book, candidate, project_response.json()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg and ffprobe are required for the W2 media acceptance gate",
)
def test_reopen_restart_retry_revoke_and_download(tmp_path: Path) -> None:
    db_path, render_dir = tmp_path / "workspace.sqlite3", tmp_path / "renders"
    first_service, first_client = _client(db_path, render_dir)
    book, candidate, project = _seed(first_client)
    run_response = first_client.post(
        f"/api/v1/creative-projects/{project['id']}/runs",
        headers={"Idempotency-Key": "w2-review-run-1"},
        json={"brief_version": 1},
    )
    assert run_response.status_code == 202, run_response.text
    run = run_response.json()
    first_service.database.close()

    # Worker process 1 creates two scripts and persists the review checkpoint.
    _worker_once(db_path, render_dir)
    reopened_service, reopened_client = _client(db_path, render_dir)
    assert reopened_client.get("/api/v1/books").json()["items"][0]["id"] == book["id"]
    assert reopened_client.get("/api/v1/creative-projects").json()["items"][0]["id"] == project["id"]
    workspace_response = reopened_client.get(
        f"/api/v1/creative-projects/{project['id']}/workspace"
    )
    assert workspace_response.status_code == 200, workspace_response.text
    workspace = workspace_response.json()
    assert workspace["book"]["id"] == book["id"]
    assert len(workspace["script_versions"]) == 2
    assert workspace["runs"][0]["status"] == "waiting_review"
    assert workspace["publication_package"] is None

    script = workspace["script_versions"][0]
    review_body = {
        "script_version_id": script["id"],
        "decision": "approve",
        "note": "虚构样例人工确认",
        "fact_confirmed": True,
        "product_match_confirmed": True,
        "asset_rights_confirmed": True,
    }
    blocked = reopened_client.post(f"/api/v1/runs/{run['id']}/review", json=review_body)
    assert blocked.status_code == 409 and blocked.json()["code"] == "product_not_confirmed"
    confirmed = reopened_client.patch(
        f"/api/v1/promotion-candidates/{candidate['id']}/eligibility",
        json={
            "eligibility_status": "manually_confirmed",
            "expected_revision": candidate["eligibility_revision"],
            "eligibility_note": "仅供虚构流程测试，非真实平台资格",
            "confirmation_basis": "creator_account_backend",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    approved = reopened_client.post(f"/api/v1/runs/{run['id']}/review", json=review_body)
    assert approved.status_code == 200, approved.text
    reopened_service.database.close()

    # Worker process 2 has no process-local memory from process 1.
    _worker_once(db_path, render_dir)
    final_service, final_client = _client(db_path, render_dir)
    assert final_client.get(f"/api/v1/runs/{run['id']}").json()["status"] == "succeeded"
    assert len(final_client.get(f"/api/v1/creative-projects/{project['id']}/scripts").json()) == 2
    events = final_service.list_run_events(run["id"])
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    replay = final_client.get(
        f"/api/v1/runs/{run['id']}/events",
        headers={"Last-Event-ID": str(events[-2]["sequence"])},
    )
    assert f"id: {events[-1]['sequence']}" in replay.text

    # A failed render is retried under the same ID and only one output is published.
    render_response = final_client.post(
        "/api/v1/renders",
        headers={"Idempotency-Key": "w2-render-0001"},
        json={
            "project_id": project["id"],
            "script_version_id": script["id"],
            "promotion_candidate_id": candidate["id"],
            "width": 1080,
            "height": 1920,
            "asset_rights": SELF_CREATED_RIGHTS,
        },
    )
    assert render_response.status_code == 202, render_response.text
    render = render_response.json()
    assert final_service.claim_next_render()["id"] == render["id"]
    final_service.fail_render(render["id"], "模拟可恢复的本地渲染失败")
    retry_url = f"/api/v1/renders/{render['id']}/retry"
    retry_headers = {"Idempotency-Key": "w2-render-retry-1"}
    retry = final_client.post(retry_url, headers=retry_headers)
    assert retry.status_code == 202 and retry.json()["attempt"] == 2, retry.text
    assert final_client.post(retry_url, headers=retry_headers).json() == retry.json()
    final_service.database.close()
    _worker_once(db_path, render_dir)

    download_service, download_client = _client(db_path, render_dir)
    finished = download_client.get(f"/api/v1/renders/{render['id']}").json()
    assert finished["status"] == "succeeded" and finished["attempt"] == 2
    probe = probe_mp4(download_service.get_render_download_path(render["id"]))
    assert probe["width"] == 1080 and probe["height"] == 1920
    assert 20 <= probe["duration_seconds"] <= 45 and not probe["audio_present"]
    assert len(list(render_dir.glob("*.mp4"))) == 1
    package = download_client.get(
        f"/api/v1/creative-projects/{project['id']}/publication-package"
    ).json()
    assert package["checklist"]["manual_attachment_required"] is True
    video = download_client.get(f"/api/v1/renders/{render['id']}/download")
    assert video.status_code == 200 and video.headers["content-type"].startswith("video/mp4")
    assert len(video.content) > 1000

    # A worker that disappears after claiming a render must not strand an
    # accepted cancellation or produce a second media file.
    cancel_render_response = download_client.post(
        "/api/v1/renders",
        headers={"Idempotency-Key": "w2-cancel-render-2"},
        json={
            "project_id": project["id"],
            "script_version_id": script["id"],
            "promotion_candidate_id": candidate["id"],
            "width": 1080,
            "height": 1920,
            "asset_rights": SELF_CREATED_RIGHTS,
        },
    )
    assert cancel_render_response.status_code == 202, cancel_render_response.text
    cancel_render = cancel_render_response.json()
    assert download_service.claim_next_render()["id"] == cancel_render["id"]
    requested = download_client.post(
        f"/api/v1/renders/{cancel_render['id']}/cancel",
        headers={"Idempotency-Key": "w2-active-render-cancel"},
        json={"reason": "虚构测试中止"},
    )
    assert requested.status_code == 200 and requested.json()["cancel_requested_at"]
    with download_service.database.transaction() as conn:
        conn.execute(
            "UPDATE render_jobs SET execution_claimed_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000Z", cancel_render["id"]),
        )
    assert process_one(download_service)
    assert download_service.get_render(cancel_render["id"])["status"] == "cancelled"
    assert len(list(render_dir.glob("*.mp4"))) == 1

    # Revocation changes the current checklist and blocks another render.
    withdrawn = download_client.patch(
        f"/api/v1/promotion-candidates/{candidate['id']}/eligibility",
        json={
            "eligibility_status": "unverified",
            "expected_revision": confirmed.json()["eligibility_revision"],
            "eligibility_note": "虚构测试中的资格撤回",
            "confirmation_basis": None,
        },
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["eligibility_checked_at"] is None
    assert withdrawn.json()["eligibility_checked_by_user_id"] is None
    refreshed_package = download_client.get(
        f"/api/v1/creative-projects/{project['id']}/publication-package"
    ).json()
    assert refreshed_package["checklist"]["product_eligible"] is False
    rejected_render = download_client.post(
        "/api/v1/renders",
        headers={"Idempotency-Key": "w2-render-blocked-2"},
        json={
            "project_id": project["id"],
            "script_version_id": script["id"],
            "promotion_candidate_id": candidate["id"],
            "width": 1080,
            "height": 1920,
            "asset_rights": SELF_CREATED_RIGHTS,
        },
    )
    assert rejected_render.status_code == 409


def test_run_cancel_and_retry_are_idempotent_across_requests(tmp_path: Path) -> None:
    service, client = _client(tmp_path / "workspace.sqlite3", tmp_path / "renders")
    _, _, project = _seed(client)
    run_url = f"/api/v1/creative-projects/{project['id']}/runs"
    run = client.post(
        run_url, headers={"Idempotency-Key": "w2-cancel-run-1"}, json={"brief_version": 1}
    ).json()
    cancel_url = f"/api/v1/runs/{run['id']}/cancel"
    headers = {"Idempotency-Key": "w2-cancel-key-1"}
    first = client.post(cancel_url, headers=headers, json={"reason": "虚构测试取消"})
    assert first.status_code == 200 and first.json()["status"] == "cancelled"
    assert client.post(cancel_url, headers=headers, json={"reason": "虚构测试取消"}).json() == first.json()
    assert client.post(cancel_url, headers=headers, json={"reason": "不同原因"}).json()["code"] == "idempotency_key_conflict"
    assert client.post(f"/api/v1/runs/{run['id']}/retry", headers={"Idempotency-Key": "w2-invalid-retry"}).status_code == 409
    assert len(service.list_run_events(run["id"])) == 2

    failed = client.post(
        run_url, headers={"Idempotency-Key": "w2-failed-run-1"}, json={"brief_version": 1}
    ).json()
    assert service.claim_next_run()["id"] == failed["id"]
    service.transition_run(failed["id"], status="failed", reason_code="demo_failure")
    retry_url = f"/api/v1/runs/{failed['id']}/retry"
    retry_headers = {"Idempotency-Key": "w2-run-retry-1"}
    retry = client.post(retry_url, headers=retry_headers)
    assert retry.status_code == 202 and retry.json()["attempt"] == 2
    assert client.post(retry_url, headers=retry_headers).json() == retry.json()
    assert service.get_run(failed["id"])["status"] == "queued"
    assert len(service.list_run_events(failed["id"])) == 4
    assert process_one(service)
    assert service.get_run(failed["id"])["status"] == "waiting_review"
    assert len(service.list_script_versions(project["id"])) == 2


def test_crashed_worker_confirms_running_run_cancellation(tmp_path: Path) -> None:
    service, client = _client(tmp_path / "workspace.sqlite3", tmp_path / "renders")
    _, _, project = _seed(client)
    run = client.post(
        f"/api/v1/creative-projects/{project['id']}/runs",
        headers={"Idempotency-Key": "w2-running-cancel"},
        json={"brief_version": 1},
    ).json()
    assert service.claim_next_run()["id"] == run["id"]
    requested = client.post(
        f"/api/v1/runs/{run['id']}/cancel",
        headers={"Idempotency-Key": "w2-cancel-after-crash"},
        json={"reason": "虚构测试中止"},
    )
    assert requested.status_code == 200 and requested.json()["cancel_requested_at"]
    with service.database.transaction() as conn:
        conn.execute(
            "UPDATE runs SET execution_claimed_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00.000Z", run["id"]),
        )
    assert process_one(service)
    assert service.get_run(run["id"])["status"] == "cancelled"
    events = service.list_run_events(run["id"])
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
