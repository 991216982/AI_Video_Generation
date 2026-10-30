"""One harmless, manually sourced book from API input to downloadable MP4."""

from __future__ import annotations

import json
import hashlib
import io
import shutil
import wave
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


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg and ffprobe are required for the actual MP4 gate",
)
def test_manual_flow_to_downloadable_mp4(tmp_path: Path) -> None:
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    service = DomainService(Database(tmp_path / "demo.sqlite3"), render_output_dir=tmp_path / "renders")
    app = FastAPI()
    app.include_router(create_api_router(service))
    client = TestClient(app)

    book_response = client.post("/api/v1/books", json=sample["book"])
    assert book_response.status_code == 201, book_response.text
    book = book_response.json()

    candidate_data = {**sample["promotion_candidate"], "book_id": book["id"]}
    candidate_response = client.post("/api/v1/promotion-candidates", json=candidate_data)
    assert candidate_response.status_code == 201, candidate_response.text
    candidate = candidate_response.json()
    assert candidate["eligibility_status"] == "unverified"
    confirmation_response = client.patch(
        f"/api/v1/promotion-candidates/{candidate['id']}/eligibility",
        json={
            "eligibility_status": "manually_confirmed",
            "expected_revision": candidate["eligibility_revision"],
            "eligibility_note": "虚构演示：账号持有人在测试情境中人工核对",
            "confirmation_basis": "creator_account_backend",
        },
    )
    assert confirmation_response.status_code == 200, confirmation_response.text
    candidate = confirmation_response.json()

    evidence_response = client.post("/api/v1/evidence", json=sample["evidence"])
    assert evidence_response.status_code == 201, evidence_response.text
    evidence = evidence_response.json()

    brief = {
        **sample["project_brief"],
        "book_id": book["id"],
        "promotion_candidate_id": candidate["id"],
        "evidence_ids": [evidence["id"]],
    }
    project_response = client.post("/api/v1/creative-projects", json=brief)
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()

    run_response = client.post(
        f"/api/v1/creative-projects/{project['id']}/runs",
        headers={"Idempotency-Key": "demo-script-run-1"},
        json={"brief_version": 1},
    )
    assert run_response.status_code == 202, run_response.text
    run = run_response.json()
    replay = client.post(
        f"/api/v1/creative-projects/{project['id']}/runs",
        headers={"Idempotency-Key": "demo-script-run-1"},
        json={"brief_version": 1},
    )
    assert replay.status_code == 202 and replay.json()["id"] == run["id"]
    assert process_one(service)
    waiting = client.get(f"/api/v1/runs/{run['id']}").json()
    assert waiting["status"] == "waiting_review", waiting
    versions = client.get(f"/api/v1/creative-projects/{project['id']}/scripts").json()
    assert len(versions) == 2
    results_response = client.get(f"/api/v1/runs/{run['id']}/generation-results")
    costs_response = client.get(f"/api/v1/runs/{run['id']}/costs")
    assert results_response.status_code == 200, results_response.text
    assert costs_response.status_code == 200, costs_response.text
    results, costs = results_response.json(), costs_response.json()
    assert len(results) == len(costs) == 2
    assert {item["script_version_id"] for item in results} == {item["id"] for item in versions}
    assert {item["generation_result_id"] for item in costs} == {item["id"] for item in results}
    assert all(item["amount_minor"] == 0 for item in costs)

    edited_segments = [dict(segment) for segment in versions[0]["segments"]]
    edited_segments[0]["onscreen_text"] = "你会怎样画出今天的路线？（创作者改稿）"
    edit_response = client.post(
        f"/api/v1/creative-projects/{project['id']}/scripts",
        json={
            "based_on_version_id": versions[0]["id"],
            "segments": edited_segments,
            "title_suggestion": versions[0]["title_suggestion"],
            "description_suggestion": versions[0]["description_suggestion"],
        },
    )
    assert edit_response.status_code == 201, edit_response.text
    edited = edit_response.json()

    review_response = client.post(
        f"/api/v1/runs/{run['id']}/review",
        json={
            "script_version_id": edited["id"],
            "decision": "approve",
            "note": "虚构样例人工确认",
            "fact_confirmed": True,
            "product_match_confirmed": True,
            "asset_rights_confirmed": True,
        },
    )
    assert review_response.status_code == 200, review_response.text
    assert process_one(service)
    approved_run = client.get(f"/api/v1/runs/{run['id']}").json()
    assert approved_run["status"] == "succeeded", approved_run
    events = service.list_run_events(run["id"])
    assert [item["sequence"] for item in events] == list(range(1, len(events) + 1))
    assert "run.review_requested" in {item["type"] for item in events}
    assert "run.reviewed" in {item["type"] for item in events}
    replay = client.get(
        f"/api/v1/runs/{run['id']}/events",
        headers={"Last-Event-ID": str(events[-2]["sequence"])},
    )
    assert replay.status_code == 200
    assert f"id: {events[-1]['sequence']}" in replay.text

    render_response = client.post(
        "/api/v1/renders",
        headers={"Idempotency-Key": "demo-render-0001"},
        json={
            "project_id": project["id"],
            "script_version_id": edited["id"],
            "promotion_candidate_id": candidate["id"],
            "width": 1080,
            "height": 1920,
            "asset_rights": SELF_CREATED_RIGHTS,
        },
    )
    assert render_response.status_code == 202, render_response.text
    render = render_response.json()
    assert process_one(service)
    finished = client.get(f"/api/v1/renders/{render['id']}").json()
    assert finished["status"] == "succeeded", finished
    probe = probe_mp4(service.get_render_download_path(render["id"]))
    assert probe["width"] == 1080 and probe["height"] == 1920
    assert 20 <= probe["duration_seconds"] <= 45
    assert not probe["audio_present"]

    package_response = client.get(f"/api/v1/creative-projects/{project['id']}/publication-package")
    assert package_response.status_code == 200, package_response.text
    package = package_response.json()
    assert package["render_id"] == render["id"]
    assert package["checklist"]["manual_attachment_required"] is True
    assert package["publication_state"] == "needs_manual_checks"
    video_response = client.get(f"/api/v1/renders/{render['id']}/download")
    assert video_response.status_code == 200, video_response.text
    assert video_response.headers["content-type"].startswith("video/mp4")
    assert len(video_response.content) > 1000

    # A synthetic WAV exercises the consented self-recording path without
    # placing a real person's voice or copyrighted audio in the repository.
    audio_buffer = io.BytesIO()
    with wave.open(audio_buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\0" * (16_000 * 2 * 30))
    audio_bytes = audio_buffer.getvalue()
    upload = client.post(
        "/api/v1/audio-assets",
        data={
            "sha256": hashlib.sha256(audio_bytes).hexdigest(),
            "origin": "self_recorded_voice",
            "rights_attested": "true",
            "rights_proof_note": "虚构演示：合成音频模拟本人录音流程，不是真实声音",
            "permitted_uses": "render_and_manual_publish",
        },
        files={"file": ("fictional.wav", audio_bytes, "audio/wav")},
    )
    assert upload.status_code == 201, upload.text
    audio_asset = upload.json()
    assert audio_asset["usage_status"] == "allowed"
    audio_render_response = client.post(
        "/api/v1/renders",
        headers={"Idempotency-Key": "demo-audio-render-0002"},
        json={
            "project_id": project["id"],
            "script_version_id": edited["id"],
            "promotion_candidate_id": candidate["id"],
            "width": 1080,
            "height": 1920,
            "audio_asset_id": audio_asset["id"],
            "asset_rights": SELF_CREATED_RIGHTS,
        },
    )
    assert audio_render_response.status_code == 202, audio_render_response.text
    audio_render = audio_render_response.json()
    assert process_one(service)
    audio_finished = client.get(f"/api/v1/renders/{audio_render['id']}").json()
    assert audio_finished["status"] == "succeeded", audio_finished
    audio_probe = probe_mp4(service.get_render_download_path(audio_render["id"]))
    assert audio_probe["width"] == 1080 and audio_probe["height"] == 1920
    assert 20 <= audio_probe["duration_seconds"] <= 45
    assert audio_probe["audio_present"]
    assert client.get(f"/api/v1/renders/{audio_render['id']}/download").status_code == 200
