"""W4-A research desk: metric evidence, references, style cards, CSV import.

All books, products, accounts, numbers and links are fictitious demonstration data.
No real account, secret, platform scrape, licensed media or verified sales figure is used.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from book_video.api.router import create_api_router
from book_video.domain.service import DomainService
from book_video.persistence.database import Database


OWNER = "00000000-0000-4000-8000-000000000090"
OTHER = "00000000-0000-4000-8000-000000000091"


def _app(tmp_path, owner: str = OWNER) -> tuple[TestClient, DomainService]:
    service = DomainService(
        Database(tmp_path / "w4.sqlite3"), owner_user_id=owner,
        render_output_dir=tmp_path / "renders",
    )
    app = FastAPI()
    app.include_router(create_api_router(service))
    return TestClient(app), service


def _evidence(client: TestClient, **overrides) -> dict:
    body = {
        "source_type": "manual",
        "source_note": "虚构演示人工观察笔记",
        "source_url": None,
        "observed_at": "2026-11-01T00:00:00Z",
        "usage_basis": "仅限本地研究参考",
        "metric_name": None,
        "metric_value": None,
        "metric_window_start": None,
        "metric_window_end": None,
        "missing_reason": "未接入获授权数据源",
    }
    body.update(overrides)
    response = client.post("/api/v1/evidence", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def _metric_body(evidence_id: str | None, **overrides) -> dict:
    body = {
        "object_type": "content_item",
        "object_ref": "DEMO-VIDEO-003",
        "object_label": "虚构演示作品",
        "platform": "douyin",
        "book_id": None,
        "metric_name": "plays",
        "metric_definition": "自有账号作品观察窗口内的播放量（虚构演示）",
        "basis": "engagement_signal",
        "unit": "views",
        "value": 8200,
        "value_text": None,
        "window_start": "2026-10-25T00:00:00Z",
        "window_end": "2026-10-31T23:59:59Z",
        "observed_at": "2026-11-01T00:40:00Z",
        "evidence_id": evidence_id,
        "missing_reason": None,
        "confidence": "medium",
        "expires_at": None,
        "note": "热度信号，不代表销量",
    }
    body.update(overrides)
    return body


def test_evidence_list_is_owner_scoped_and_searchable(tmp_path) -> None:
    client, _ = _app(tmp_path)
    _evidence(client, source_note="虚构：地图观察 A")
    _evidence(client, source_note="虚构：另一本观察 B", source_url="https://example.invalid/note")
    page = client.get("/api/v1/evidence", params={"q": "地图"})
    assert page.status_code == 200
    assert len(page.json()["items"]) == 1
    typed = client.get("/api/v1/evidence", params={"source_type": "manual"})
    assert typed.status_code == 200
    assert len(typed.json()["items"]) == 2


def test_numeric_observation_requires_evidence_definition_and_window(tmp_path) -> None:
    client, _ = _app(tmp_path)
    evidence = _evidence(client)

    # Numeric value without evidence is rejected.
    missing_evidence = client.post("/api/v1/metrics", json=_metric_body(None))
    assert missing_evidence.status_code == 422
    assert missing_evidence.json()["code"] == "validation_error"

    # Windowed basis without a bounded window is rejected.
    no_window = client.post(
        "/api/v1/metrics",
        json=_metric_body(evidence["id"], window_start=None, window_end=None),
    )
    assert no_window.status_code == 422

    # A valid engagement-signal observation is accepted.
    ok = client.post("/api/v1/metrics", json=_metric_body(evidence["id"]))
    assert ok.status_code == 201, ok.text
    metric = ok.json()
    assert metric["basis"] == "engagement_signal"
    assert metric["legacy_backfill"] is False

    # Order-sales basis is the only one that may be presented as actual sales.
    order = client.post(
        "/api/v1/metrics",
        json=_metric_body(
            evidence["id"], object_type="product_listing", object_ref="DEMO-PRODUCT-001",
            metric_name="sales_units", basis="authorized_order_sales", unit="count",
            value=42, object_label="虚构演示商品",
        ),
    )
    assert order.status_code == 201, order.text
    assert order.json()["basis"] == "authorized_order_sales"

    # Estimate basis is kept distinct from order sales.
    estimate = client.post(
        "/api/v1/metrics",
        json=_metric_body(
            evidence["id"], object_type="creator_account", object_ref="DEMO-ACCOUNT-002",
            metric_name="follower_count", basis="third_party_or_manual_estimate", unit="count",
            value=15000, object_label="虚构演示账号", platform="kuaishou",
        ),
    )
    assert estimate.status_code == 201
    assert estimate.json()["basis"] == "third_party_or_manual_estimate"


def test_missing_observation_keeps_a_reason_and_zero_is_not_unknown(tmp_path) -> None:
    client, _ = _app(tmp_path)
    evidence = _evidence(client)

    # Null value without a reason is rejected.
    null_no_reason = client.post(
        "/api/v1/metrics",
        json=_metric_body(
            None, value=None, missing_reason=None, metric_name="sales_units",
            basis="legacy_unclassified", unit="count", window_start=None, window_end=None,
            object_type="book_edition", object_ref="DEMO-BOOK-011",
        ),
    )
    assert null_no_reason.status_code == 422

    # Null with a reason and no evidence is accepted as "not obtained".
    missing = client.post(
        "/api/v1/metrics",
        json=_metric_body(
            None, value=None, missing_reason="未取得授权订单数据，也无可信估算",
            metric_name="sales_units", basis="legacy_unclassified", unit="count",
            window_start=None, window_end=None, object_type="book_edition",
            object_ref="DEMO-BOOK-011", observed_at="2026-11-01T00:45:00Z",
        ),
    )
    assert missing.status_code == 201, missing.text
    assert missing.json()["value"] is None
    assert missing.json()["missing_reason"]

    # A real measured zero is a valued observation that still needs evidence.
    zero_without_evidence = client.post(
        "/api/v1/metrics",
        json=_metric_body(
            None, value=0, metric_name="likes", basis="engagement_signal",
        ),
    )
    assert zero_without_evidence.status_code == 422


def test_metric_references_are_owner_scoped(tmp_path) -> None:
    client, service = _app(tmp_path)
    evidence = _evidence(client)
    metric = client.post("/api/v1/metrics", json=_metric_body(evidence["id"])).json()

    other = DomainService(service.database, owner_user_id=OTHER, render_output_dir=service.render_output_dir)
    try:
        other.get_metric_observation(metric["id"])
    except Exception as error:
        assert getattr(error, "status_code", None) == 404
    else:
        raise AssertionError("another owner read the metric")
    response = client.get(f"/api/v1/metrics/{metric['id']}")
    assert response.status_code == 200


def test_content_reference_rejects_local_paths_and_enforces_material_declaration(tmp_path) -> None:
    client, _ = _app(tmp_path)
    evidence = _evidence(client)

    bad_url = client.post(
        "/api/v1/content-references",
        json={
            "ref_type": "content_item", "title": "虚构观察", "license_scope": "own_account",
            "usage_basis": "自有账号复盘", "reference_url": "file:///C:/secret/clip.mp4",
            "evidence_ids": [evidence["id"]],
            "material_statement": "no_third_party_media_or_verbatim_transcript",
        },
    )
    assert bad_url.status_code == 422

    bad_declaration = client.post(
        "/api/v1/content-references",
        json={
            "ref_type": "content_item", "title": "虚构观察", "license_scope": "own_account",
            "usage_basis": "自有账号复盘", "reference_url": "https://example.invalid/v1",
            "evidence_ids": [evidence["id"]],
            "material_statement": None,
        },
    )
    assert bad_declaration.status_code == 422

    good = client.post(
        "/api/v1/content-references",
        json={
            "ref_type": "content_item", "title": "自有作品抽象观察",
            "license_scope": "own_account", "usage_basis": "自有账号复盘与风格提炼",
            "reference_url": "https://example.invalid/v1", "note": "前 3 秒反常识提问，未抄录字幕",
            "evidence_ids": [evidence["id"]], "observed_at": "2026-11-01T00:40:00Z",
            "material_statement": "no_third_party_media_or_verbatim_transcript",
        },
    )
    assert good.status_code == 201, good.text
    reference = good.json()
    assert reference["material_statement"] == "no_third_party_media_or_verbatim_transcript"


def test_style_card_requires_evidence_to_approve_and_tracks_revisions(tmp_path) -> None:
    client, _ = _app(tmp_path)
    evidence = _evidence(client)

    # Evidence-free draft exists but cannot be approved.
    draft = client.post(
        "/api/v1/style-cards",
        json={"title": "待补证据草稿", "features": [], "evidence_ids": []},
    )
    assert draft.status_code == 201
    assert draft.json()["review_status"] == "draft"
    approve_draft = client.post(f"/api/v1/style-cards/{draft.json()['id']}/review", json={"decision": "approve"})
    assert approve_draft.status_code == 422

    inflated = client.post(
        "/api/v1/style-cards",
        json={
            "title": "虚构的样本数膨胀卡",
            "evidence_ids": [evidence["id"]],
            "features": [{
                "feature_key": "opening_hook", "label": "抽象开场", "explanation": "仅作测试",
                "evidence_ids": [evidence["id"]], "sample_count": 2,
            }],
        },
    )
    assert inflated.status_code == 422

    card = client.post(
        "/api/v1/style-cards",
        json={
            "title": "反常识开场 + 具体方法收尾", "target_platform": "douyin",
            "derived_by": "manual",
            "features": [{
                "feature_key": "opening_hook", "label": "反常识提问开场",
                "explanation": "前 3 秒提出反常识问题吸引停留，不引用原句。",
                "evidence_ids": [evidence["id"]], "sample_count": 1,
            }],
            "evidence_ids": [evidence["id"]], "human_note": "人工复核：抽象特征，未复制第三方素材",
        },
    )
    assert card.status_code == 201, card.text
    body = card.json()
    assert body["review_status"] == "pending_review"
    assert body["abstract_only"] is True
    assert body["evidence_sample_count"] == 1

    # Reject without a note is rejected; approval with evidence works.
    reject_no_note = client.post(f"/api/v1/style-cards/{body['id']}/review", json={"decision": "reject"})
    assert reject_no_note.status_code == 422
    approved = client.post(
        f"/api/v1/style-cards/{body['id']}/review",
        json={"decision": "approve", "note": "证据充分，特征为抽象模式"},
    )
    assert approved.status_code == 200
    assert approved.json()["review_status"] == "approved"

    # Revision conflict on a stale expected_revision.
    stale = client.patch(
        f"/api/v1/style-cards/{body['id']}",
        json={"expected_revision": body["revision"] + 1, "title": "改后标题"},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "revision_conflict"

    revised = client.patch(
        f"/api/v1/style-cards/{body['id']}",
        json={"expected_revision": body["revision"], "title": "反常识开场（修订）"},
    )
    assert revised.status_code == 200
    assert revised.json()["revision"] == body["revision"] + 1
    # A human revision after approval returns the card to pending review.
    assert revised.json()["review_status"] == "pending_review"


def _csv_bytes() -> bytes:
    header = "isbn,title,edition_label,author,platform,platform_product_id,product_url,metric_name,basis,value,observed_at"
    rows = [
        "978-0-0000-0001-1,把日子写成地图,虚构演示版 1.0,林例,douyin,DEMO-PRODUCT-001,https://example.invalid/demo-product,plays,engagement_signal,8200,2026-11-01T00:40:00Z",
        "978-0-0000-0001-1,把日子写成地图,虚构演示版 1.0,林例,douyin,DEMO-PRODUCT-002,https://example.invalid/demo-product-002,sales_units,authorized_order_sales,约四十多,2026-11-01T00:10:00Z",
        "978-0-0000-0002-2,另一本虚构样书,虚构演示版 2.0,王例,kuaishou,DEMO-PRODUCT-003,=cmd|/c calc 'A1',listed_price,platform_listing_display,39.00,2026-11-01T00:20:00Z",
        "978-0-0000-0001-1,把日子写成地图,虚构演示版 1.0,林例,douyin,DEMO-PRODUCT-001,https://example.invalid/demo-product,follower_count,third_party_or_manual_estimate,15000,2026-11-01T00:30:00Z",
        "978-0-0000-0003-3,把日子写成地图,虚构演示版 精装,林例,douyin,DEMO-PRODUCT-004,https://example.invalid/demo-product-004,plays,engagement_signal,5000,2026-11-01T00:35:00Z",
        "978-0-0000-0004-4,第三本虚构样书,虚构演示版 1.0,赵例,kuaishou,DEMO-PRODUCT-005,https://example.invalid/demo-product-005,likes,engagement_signal,1200,2026-11-01 00:30",
    ]
    return b"\xef\xbb\xbf" + ("\r\n".join([header, *rows]) + "\r\n").encode("utf-8")


def test_csv_preview_detects_bad_rows_duplicates_and_edition_mismatch(tmp_path) -> None:
    client, _ = _app(tmp_path)
    response = client.post(
        "/api/v1/imports/preview",
        files={"file": ("fictitious.csv", _csv_bytes(), "text/csv")},
        headers={"Idempotency-Key": "preview-key-0001"},
    )
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["status"] == "preview"
    assert preview["encoding_detected"] == "utf8_bom"
    assert preview["total_rows"] == 6
    codes = {(d["row_number"], d["code"]) for d in preview["row_diagnostics"]}
    assert (2, "invalid_number") in codes
    assert (3, "formula_injection") in codes
    assert (4, "duplicate_natural_key") in codes
    assert (5, "edition_mismatch_suspected") in codes
    assert (6, "invalid_timestamp") in codes

    # Replaying the same file with the same key returns the same preview.
    replay = client.post(
        "/api/v1/imports/preview",
        files={"file": ("fictitious.csv", _csv_bytes(), "text/csv")},
        headers={"Idempotency-Key": "preview-key-0001"},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == preview["id"]

    # Same key with a different request is a conflict.
    conflict = client.post(
        "/api/v1/imports/preview",
        files={"file": ("other.csv", b"a,b\n1,2\n", "text/csv")},
        headers={"Idempotency-Key": "preview-key-0001"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_key_conflict"


def test_csv_confirm_applies_valid_rows_partially_and_is_idempotent(tmp_path) -> None:
    client, service = _app(tmp_path)
    preview = client.post(
        "/api/v1/imports/preview",
        files={"file": ("fictitious.csv", _csv_bytes(), "text/csv")},
        headers={"Idempotency-Key": "preview-key-0002"},
    ).json()

    confirmed = client.post(
        f"/api/v1/imports/{preview['id']}/confirm",
        headers={"Idempotency-Key": "confirm-key-0001"},
        json={"mapping": preview["mapping_suggestions"], "on_duplicate": "skip",
              "match_by": "isbn_and_edition", "normalize_isbn": True},
    )
    assert confirmed.status_code == 200, confirmed.text
    job = confirmed.json()
    assert job["status"] == "partially_applied"
    assert job["counts"]["failed"] == 3
    assert job["counts"]["skipped_duplicate"] >= 1
    assert "book_edition" in job["entity_kinds"]
    assert "metric_observation" in job["entity_kinds"]

    # Same title/author but different edition was not auto-merged.
    books = client.get("/api/v1/books").json()["items"]
    editions = {(b["title"], b["edition_label"]) for b in books}
    assert ("把日子写成地图", "虚构演示版 精装") in editions

    # Applying twice is rejected; the first confirm replay with its key is stable.
    again = client.post(
        f"/api/v1/imports/{preview['id']}/confirm",
        headers={"Idempotency-Key": "confirm-key-0003"},
        json={"mapping": preview["mapping_suggestions"]},
    )
    assert again.status_code == 409
    assert again.json()["code"] == "import_already_applied"

    other = DomainService(service.database, owner_user_id=OTHER, render_output_dir=service.render_output_dir)
    try:
        other.get_import(preview["id"])
    except Exception as error:
        assert getattr(error, "status_code", None) == 404
    else:
        raise AssertionError("another owner read the import job")


def test_csv_confirm_uses_edited_mapping_and_rejects_ambiguous_targets(tmp_path) -> None:
    client, _ = _app(tmp_path)
    csv_bytes = "title,出版社,author,edition_label\n虚构新书,演示出版社,虚构作者,演示版\n".encode("utf-8")
    preview = client.post(
        "/api/v1/imports/preview",
        files={"file": ("edited-mapping.csv", csv_bytes, "text/csv")},
        headers={"Idempotency-Key": "edited-mapping-preview"},
    ).json()
    assert preview["mapping_suggestions"]["出版社"] is None

    unknown = client.post(
        f"/api/v1/imports/{preview['id']}/confirm",
        headers={"Idempotency-Key": "edited-mapping-unknown"},
        json={"mapping": {**preview["mapping_suggestions"], "extra": "book.publisher"}},
    )
    assert unknown.status_code == 422
    assert unknown.json()["code"] == "import_mapping_invalid"

    duplicate_mapping = dict(preview["mapping_suggestions"])
    duplicate_mapping["出版社"] = "book.title"
    duplicate = client.post(
        f"/api/v1/imports/{preview['id']}/confirm",
        headers={"Idempotency-Key": "edited-mapping-duplicate"},
        json={"mapping": duplicate_mapping},
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["code"] == "import_mapping_invalid"

    mapping = dict(preview["mapping_suggestions"])
    mapping["出版社"] = "book.publisher"
    confirmed = client.post(
        f"/api/v1/imports/{preview['id']}/confirm",
        headers={"Idempotency-Key": "edited-mapping-confirm"},
        json={"mapping": mapping},
    )
    assert confirmed.status_code == 200, confirmed.text
    books = client.get("/api/v1/books").json()["items"]
    assert len(books) == 1
    assert books[0]["publisher"] == "演示出版社"


def test_csv_isbn_only_mode_does_not_overwrite_another_edition(tmp_path) -> None:
    client, _ = _app(tmp_path)
    header = "isbn,title,edition_label,author\n"
    first = (header + "978-0-0000-0099-9,虚构版次书,演示版 A,虚构作者\n").encode("utf-8")
    second = (header + "978-0-0000-0099-9,虚构版次书,演示版 B,虚构作者\n").encode("utf-8")
    for index, content in enumerate((first, second), start=1):
        preview = client.post(
            "/api/v1/imports/preview",
            files={"file": (f"edition-{index}.csv", content, "text/csv")},
            headers={"Idempotency-Key": f"edition-preview-{index}"},
        ).json()
        confirmed = client.post(
            f"/api/v1/imports/{preview['id']}/confirm",
            headers={"Idempotency-Key": f"edition-confirm-{index}"},
            json={"mapping": preview["mapping_suggestions"], "match_by": "isbn", "on_duplicate": "update"},
        )
        if index == 1:
            assert confirmed.status_code == 200, confirmed.text
        else:
            assert confirmed.status_code == 422
            assert confirmed.json()["code"] == "edition_conflict"
    books = client.get("/api/v1/books").json()["items"]
    assert len(books) == 1
    assert books[0]["edition_label"] == "演示版 A"


def test_workflow_style_card_is_evidence_backed_and_feeds_approved_guidance(tmp_path) -> None:
    client, service = _app(tmp_path)
    evidence = _evidence(client, source_note="开场用提问吸引停留；语气像朋友推荐；结尾邀请分享方法。")
    reference = client.post(
        "/api/v1/content-references",
        json={
            "ref_type": "content_item", "title": "同类作品观察",
            "license_scope": "manual_observation", "usage_basis": "人工观察笔记",
            "note": "画面用自制图形，字幕短句。", "evidence_ids": [],
            "material_statement": "no_third_party_media_or_verbatim_transcript",
        },
    ).json()

    card = service.create_workflow_style_card(
        title="证据提炼风格卡", evidence_ids=[evidence["id"]], reference_ids=[reference["id"]],
    )
    assert card["derived_by"] == "workflow_extraction"
    assert card["review_status"] in {"draft", "pending_review"}
    assert card["features"], "evidence-backed features must be extracted"
    assert {ref for feature in card["features"] for ref in feature["evidence_ids"]} == {evidence["id"]}
    assert card["reference_ids"] == [reference["id"]]

    # Reference-only patterns do not become features without evidence backing.
    assert all(feature["evidence_ids"] for feature in card["features"])

    # Before approval the card must not influence generation guidance.
    assert service.list_approved_style_guidance() == []

    approved = client.post(
        f"/api/v1/style-cards/{card['id']}/review",
        json={"decision": "approve", "note": "特征为抽象模式，已人工核对"},
    )
    assert approved.status_code == 200
    guidance = service.list_approved_style_guidance()
    assert {item["feature_key"] for item in guidance} == {feature["feature_key"] for feature in card["features"]}
    # Guidance carries only abstract keys/labels/sample counts, no note text.
    for item in guidance:
        assert set(item.keys()) == {"feature_key", "label", "sample_count"}
        assert "开场用提问" not in item["label"]


def test_legacy_evidence_metric_is_backfilled_as_unclassified(tmp_path) -> None:
    import json
    import sqlite3

    path = tmp_path / "legacy-w4.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    migration_dir = Path(__file__).parents[2] / "migrations"
    for version in ("001_initial", "002_w2_reliability", "003_render_execution_lease",
                    "004_w3_generation_audio", "005_w3_invocation_recovery"):
        conn.executescript((migration_dir / f"{version}.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                     (version, "2026-10-29T00:00:00.000Z"))
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    evidence_payload = {
        "id": "00000000-0000-4000-8000-000000000301",
        "owner_user_id": OWNER, "collected_at": now,
        "source_type": "manual", "source_note": "v0.3 旧证据记录", "source_url": None,
        "observed_at": now, "usage_basis": "演示", "metric_name": "plays",
        "metric_value": 1200, "metric_window_start": None, "metric_window_end": None,
        "missing_reason": None,
    }
    conn.execute(
        "INSERT INTO evidence(id, owner_user_id, collected_at, payload) VALUES (?, ?, ?, ?)",
        (evidence_payload["id"], OWNER, now, json.dumps(evidence_payload)),
    )
    conn.commit()
    conn.close()

    reopened = DomainService(Database(path), owner_user_id=OWNER)
    with reopened.database.read() as read_conn:
        rows = read_conn.execute("SELECT payload FROM metric_observations WHERE owner_user_id = ?",
                                 (OWNER,)).fetchall()
    assert len(rows) == 1
    backfilled = json.loads(rows[0]["payload"])
    assert backfilled["basis"] == "legacy_unclassified"
    assert bool(backfilled["legacy_backfill"]) is True
    assert backfilled["value"] == 1200
