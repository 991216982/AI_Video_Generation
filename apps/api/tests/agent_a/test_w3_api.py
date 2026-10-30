"""W3 contract checks using fictional records and generated audio bytes only."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from book_video.api.router import create_api_router
from book_video.domain.service import DomainService
from book_video.persistence.database import Database


OWNER = "00000000-0000-4000-8000-000000000090"


def setup(tmp_path):
    service = DomainService(Database(tmp_path / "w3.sqlite3"), owner_user_id=OWNER,
                            render_output_dir=tmp_path / "renders")
    app = FastAPI()
    app.include_router(create_api_router(service))
    return TestClient(app), service


def book_body():
    return {"title": "虚构旅行书", "edition_label": "演示版 1", "author": "例作者",
            "isbn": None, "publisher": None, "original_opinion": "虚构的原创读书观点。",
            "facts": [{"key": "title", "value": "虚构旅行书",
                       "verification_status": "human_confirmed", "source_note": "虚构样例人工核对",
                       "evidence_id": None}]}


def test_candidate_creation_is_unverified_then_manual_patch(tmp_path):
    client, _ = setup(tmp_path)
    book = client.post("/api/v1/books", json=book_body()).json()
    base = {"book_id": book["id"], "platform": "douyin", "platform_product_id": "DEMO-NOT-REAL",
            "product_url": "https://example.invalid/demo"}
    for extra in ({"eligibility_status": "manually_confirmed"},
                  {"eligibility_checked_at": datetime.now(timezone.utc).isoformat()},
                  {"confirmation_basis": "creator_account_backend"}):
        response = client.post("/api/v1/promotion-candidates", json={**base, **extra})
        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"
    created = client.post("/api/v1/promotion-candidates", json=base)
    assert created.status_code == 201
    candidate = created.json()
    assert candidate["eligibility_status"] == "unverified"
    confirmed = client.patch(f"/api/v1/promotion-candidates/{candidate['id']}/eligibility",
                             json={"eligibility_status": "manually_confirmed", "expected_revision": 1,
                                   "eligibility_note": "虚构账号后台人工确认", "confirmation_basis": "creator_account_backend"})
    assert confirmed.status_code == 200
    assert confirmed.json()["eligibility_revision"] == 2


def test_fact_revisions_append_and_owner_scope(tmp_path):
    client, service = setup(tmp_path)
    book = client.post("/api/v1/books", json=book_body()).json()
    assert book["facts"][0]["revision"] == 1
    path = f"/api/v1/books/{book['id']}/facts/title"
    request = {"expected_revision": 1, "value": "虚构旅行书·新版",
               "verification_status": "human_confirmed", "source_note": "虚构样例重新核对", "evidence_id": None}
    changed = client.patch(path, json=request)
    assert changed.status_code == 200
    assert changed.json()["revision"] == 2
    assert client.patch(path, json=request).json()["code"] == "revision_conflict"
    history = client.get(path + "/revisions").json()
    assert [row["revision"] for row in history] == [1, 2]
    assert history[0]["value"] == "虚构旅行书"
    assert client.patch(path, json={**request, "expected_revision": 2,
                                    "verification_status": "source_verified", "source_note": None}).status_code == 422
    other = DomainService(service.database, owner_user_id=str(uuid4()),
                          render_output_dir=tmp_path / "renders")
    assert other.list_books()["items"] == []
    from book_video.domain.errors import DomainError
    try:
        other.list_book_fact_revisions(book["id"], "title")
    except DomainError as error:
        assert error.status_code == 404
    else:
        raise AssertionError("Cross-owner fact history was exposed")


def test_audio_upload_rechecks_sha_media_rights_and_owner(tmp_path, monkeypatch):
    assert shutil.which("ffprobe"), "ffprobe is required for W3 media validation"
    client, service = setup(tmp_path)
    wav = io.BytesIO()
    with wave.open(wav, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(8000)
        file.writeframes(b"\x00\x00" * 8000)
    payload = wav.getvalue()
    form = {"sha256": hashlib.sha256(payload).hexdigest(), "origin": "self_recorded_voice",
            "rights_attested": "true", "rights_proof_note": "虚构演示：测试者本人录音声明",
            "permitted_uses": "render_and_manual_publish"}
    original_replace = Path.replace

    def only_same_directory_replace(source: Path, target: Path) -> Path:
        # Emulate Windows' cross-volume rename restriction even when pytest's
        # temporary and workspace directories happen to be on one drive.
        if source.parent != Path(target).parent:
            raise OSError(17, "Cross-device link")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", only_same_directory_replace)
    bad = client.post("/api/v1/audio-assets", data={**form, "sha256": "0" * 64},
                      files={"file": ("demo.wav", payload, "audio/wav")})
    assert bad.status_code == 422
    uploaded = client.post("/api/v1/audio-assets", data=form,
                           files={"file": ("demo.wav", payload, "audio/wav")})
    assert uploaded.status_code == 201, uploaded.text
    asset = uploaded.json()
    assert asset["usage_status"] == "allowed" and asset["mime_type"] == "audio/wav"
    assert service.get_audio_asset_media(asset["id"]).is_file()
    audio_dir = service.render_output_dir.parent / "audio" / OWNER
    assert not list(audio_dir.glob("*.audio"))
    before = set(audio_dir.iterdir())

    def failed_replace(source: Path, target: Path) -> Path:
        raise OSError(5, "Simulated destination failure")

    monkeypatch.setattr(Path, "replace", failed_replace)
    with pytest.raises(OSError):
        service.upload_audio_asset(payload, sha256=form["sha256"],
                                   origin="self_recorded_voice", rights_attested=True,
                                   rights_proof_note="虚构演示：测试者本人录音声明",
                                   permitted_uses="render_and_manual_publish")
    assert set(audio_dir.iterdir()) == before
    monkeypatch.setattr(Path, "replace", only_same_directory_replace)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    rejected = client.post("/api/v1/audio-assets", data={**form, "origin": "licensed_voice",
                                                    "expires_at": expired},
                           files={"file": ("demo.wav", payload, "audio/wav")})
    assert rejected.status_code == 422 and rejected.json()["code"] == "asset_rights_blocked"
    other = DomainService(service.database, owner_user_id=str(uuid4()),
                          render_output_dir=tmp_path / "renders")
    from book_video.domain.errors import DomainError
    try:
        other.get_audio_asset_media(asset["id"])
    except DomainError as error:
        assert error.status_code == 404
    else:
        raise AssertionError("Cross-owner audio was exposed")


def test_generation_cost_idempotent_owner_scoped_and_event_sequence(tmp_path):
    client, service = setup(tmp_path)
    book = client.post("/api/v1/books", json=book_body()).json()
    candidate = client.post("/api/v1/promotion-candidates", json={
        "book_id": book["id"], "platform": "douyin", "platform_product_id": "DEMO-NOT-REAL",
        "product_url": "https://example.invalid/demo"}).json()
    project = client.post("/api/v1/creative-projects", json={
        "book_id": book["id"], "promotion_candidate_id": candidate["id"],
        "target_audience": "虚构读者", "target_platform": "douyin", "desired_seconds": 30,
        "original_angle": "原创演示观点", "prohibited_claims": [], "evidence_ids": []}).json()
    run = client.post(f"/api/v1/creative-projects/{project['id']}/runs",
                      headers={"Idempotency-Key": "w3-generation-run"},
                      json={"brief_version": 1}).json()
    service.transition_run(run["id"], status="running", stage="script")
    result_request = {"run_id": run["id"], "invocation_key": f"{run['id']}:script:a:deterministic_fixture",
                      "source": "deterministic_fixture", "provider_key": "local_fixture",
                      "model_key": "fixture_v1", "status": "succeeded", "reason_code": None,
                      "input_digest": "a" * 64, "output_digest": "b" * 64,
                      "script_version_id": str(uuid4())}
    cost_request = {"currency": "CNY", "amount_minor": 0, "missing_reason": None,
                    "input_tokens": 0, "output_tokens": 0}
    first = service.persist_generation(result_request, cost_request)
    second = service.persist_generation(result_request, cost_request)
    assert first == second
    assert len(client.get(f"/api/v1/runs/{run['id']}/generation-results").json()) == 1
    assert len(client.get(f"/api/v1/runs/{run['id']}/costs").json()) == 1
    from book_video.domain.errors import DomainError
    try:
        service.persist_generation({**result_request, "output_digest": "c" * 64}, cost_request)
    except DomainError as error:
        assert error.code == "idempotency_key_conflict"
    else:
        raise AssertionError("Changed invocation replay was accepted")
    current = service.get_run(run["id"])
    event = {"event_id": str(uuid4()), "run_id": run["id"], "project_id": project["id"],
             "sequence": current["sequence"] + 1, "type": "run.generation_completed",
             "status": "running", "stage": "script", "occurred_at": datetime.now(timezone.utc).isoformat(),
             "attempt": 1, "generation_result_id": first["generation_result"]["id"]}
    assert service.persist_run_event(event)["sequence"] == event["sequence"]
    assert service.persist_run_event(event)["sequence"] == event["sequence"]
    other = DomainService(service.database, owner_user_id=str(uuid4()))
    try:
        other.list_run_costs(run["id"])
    except DomainError as error:
        assert error.status_code == 404
    else:
        raise AssertionError("Cross-owner cost was exposed")


def test_w3_migration_backfills_old_fact_and_revokes_unaudited_confirmation(tmp_path):
    db_path = tmp_path / "old.sqlite3"
    migrations = Path(__file__).parents[2] / "migrations"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version in ("001_initial", "002_w2_reliability", "003_render_execution_lease"):
        conn.executescript((migrations / f"{version}.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, "2026-10-29T00:00:00Z"))
    book_id, candidate_id = str(uuid4()), str(uuid4())
    book = {**book_body(), "id": book_id, "owner_user_id": OWNER,
            "created_at": "2026-10-29T00:00:00Z"}
    candidate = {"id": candidate_id, "owner_user_id": OWNER, "book_id": book_id,
                 "created_at": "2026-10-29T00:01:00Z", "platform": "douyin",
                 "platform_product_id": "DEMO-NOT-REAL", "product_url": "https://example.invalid/demo",
                 "eligibility_status": "manually_confirmed",
                 "eligibility_checked_at": "2026-10-29T00:01:00Z",
                 "eligibility_note": "旧版缺少确认依据", "eligibility_revision": 1,
                 "eligibility_checked_by_user_id": OWNER}
    conn.execute("INSERT INTO books(id,owner_user_id,created_at,payload) VALUES (?,?,?,?)",
                 (book_id, OWNER, book["created_at"], json.dumps(book, ensure_ascii=False)))
    conn.execute("INSERT INTO promotion_candidates(id,owner_user_id,book_id,created_at,payload,eligibility_revision,eligibility_checked_by_user_id) VALUES (?,?,?,?,?,?,?)",
                 (candidate_id, OWNER, book_id, candidate["created_at"],
                  json.dumps(candidate, ensure_ascii=False), 1, OWNER))
    conn.commit()
    conn.close()
    service = DomainService(Database(db_path), owner_user_id=OWNER)
    migrated = service.get_book(book_id)
    assert migrated["facts"][0]["revision"] == 1
    history = service.list_book_fact_revisions(book_id, "title")
    assert len(history) == 1 and history[0]["revision"] == 1
    downgraded = service.get_promotion_candidate(candidate_id)
    assert downgraded["eligibility_status"] == "unverified"
    assert downgraded["eligibility_checked_at"] is None
    assert downgraded["eligibility_checked_by_user_id"] is None
    assert downgraded["eligibility_revision"] == 2
