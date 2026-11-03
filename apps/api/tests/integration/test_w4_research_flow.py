"""W4 research desk end-to-end: evidence, four-basis metrics, references,
evidence-backed style card, and a fictitious CSV partial import.

All books, products, accounts, links and numbers are invented; no platform
scrape, real account, secret or licensed media is used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from book_video.api.router import create_api_router
from book_video.domain.service import DomainService
from book_video.persistence.database import Database


ROOT = Path(__file__).resolve().parents[4]
FIXTURE_CSV = ROOT / "packages" / "contracts" / "examples" / "research-import-fixture.csv"
OWNER_ID = "00000000-0000-4000-8000-000000000001"
OTHER_ID = "00000000-0000-4000-8000-000000000002"


def _evidence_body(**overrides) -> dict:
    body = {
        "source_type": "manual",
        "source_note": "开场用提问吸引停留；语气像朋友推荐；结尾邀请观众分享方法。",
        "source_url": None,
        "observed_at": "2026-11-01T00:00:00Z",
        "usage_basis": "仅限本人本地研究参考",
        "metric_name": None,
        "metric_value": None,
        "metric_window_start": None,
        "metric_window_end": None,
        "missing_reason": "未取得授权数据源",
    }
    body.update(overrides)
    return body


def _metric_body(evidence_id: str | None, **overrides) -> dict:
    body = {
        "platform": "douyin",
        "book_id": None,
        "object_type": "content_item",
        "object_ref": "DEMO-VIDEO-003",
        "object_label": "虚构演示作品",
        "metric_name": "plays",
        "metric_definition": "自有作品观察窗口内的播放量（虚构演示）",
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


def _app(tmp_path: Path, owner: str = OWNER_ID) -> tuple[TestClient, DomainService]:
    service = DomainService(
        Database(tmp_path / "w4-integration.sqlite3"),
        owner_user_id=owner,
        render_output_dir=tmp_path / "renders",
    )
    app = FastAPI()
    app.include_router(create_api_router(service))
    return TestClient(app), service


def test_w4_research_loop_from_evidence_to_approved_style_guidance(tmp_path: Path) -> None:
    client, service = _app(tmp_path)

    # Book + candidate + brief so a generation snapshot can carry style guidance.
    book_response = client.post(
        "/api/v1/books",
        json={
            "title": "把日子写成地图",
            "edition_label": "虚构演示版 1.0",
            "author": "林例",
            "isbn": None,
            "publisher": None,
            "original_opinion": "我喜欢用地图比喻日常选择；虚构样书仅演示原创观点。",
            "facts": [{
                "key": "title",
                "value": "把日子写成地图",
                "verification_status": "human_confirmed",
                "source_note": "虚构演示数据，非真实图书",
                "evidence_id": None,
            }],
        },
    )
    assert book_response.status_code == 201, book_response.text
    book = book_response.json()

    candidate = client.post(
        "/api/v1/promotion-candidates",
        json={
            "book_id": book["id"],
            "platform": "douyin",
            "platform_product_id": "DEMO-NOT-REAL",
            "product_url": "https://example.invalid/demo-product",
            "eligibility_status": "unverified",
            "eligibility_checked_at": None,
            "eligibility_note": "演示占位，不可发布或挂车",
        },
    ).json()

    evidence = client.post("/api/v1/evidence", json=_evidence_body()).json()

    # Four distinct bases; only an authorized order figure may be presented as sales.
    order = client.post("/api/v1/metrics", json=_metric_body(
        evidence["id"], object_type="product_listing", object_ref="DEMO-PRODUCT-001",
        metric_name="sales_units", metric_definition="授权店铺订单口径（虚构计数）",
        basis="authorized_order_sales", unit="count", value=42,
        object_label="虚构演示商品",
    ))
    assert order.status_code == 201, order.text

    display = client.post("/api/v1/metrics", json=_metric_body(
        None, object_type="product_listing", object_ref="DEMO-PRODUCT-001",
        metric_name="displayed_sales_label", metric_definition="页面展示档位文案",
        basis="platform_listing_display", unit="text", value=None, value_text="已售10万+",
        observed_at="2026-11-01T00:12:00Z", window_start=None, window_end=None,
        missing_reason="页面仅展示档位，无订单数值",
    ))
    assert display.status_code == 201, display.text

    estimate = client.post("/api/v1/metrics", json=_metric_body(
        evidence["id"], object_type="creator_account", object_ref="DEMO-ACCOUNT-002",
        metric_name="follower_count", metric_definition="人工估算粉丝量级",
        basis="third_party_or_manual_estimate", unit="count", value=15000,
        object_label="虚构演示账号", platform="kuaishou",
    ))
    assert estimate.status_code == 201, estimate.text

    engagement = client.post("/api/v1/metrics", json=_metric_body(evidence["id"])).json()

    metrics = client.get("/api/v1/metrics").json()["items"]
    assert {item["basis"] for item in metrics} == {
        "authorized_order_sales", "platform_listing_display",
        "third_party_or_manual_estimate", "engagement_signal",
    }
    order_only = client.get("/api/v1/metrics", params={"basis": "authorized_order_sales"}).json()["items"]
    assert len(order_only) == 1 and order_only[0]["value"] == 42
    display_row = next(item for item in metrics if item["basis"] == "platform_listing_display")
    assert display_row["value"] is None and display_row["value_text"] == "已售10万+" and display_row["missing_reason"]

    # A local file path / media blob is rejected as a reference URL.
    bad_reference = client.post(
        "/api/v1/content-references",
        json={
            "ref_type": "content_item", "title": "本地路径参考",
            "license_scope": "manual_observation", "usage_basis": "测试拒绝本地路径",
            "reference_url": "file:///C:/secret/clip.mp4",
            "material_statement": "no_third_party_media_or_verbatim_transcript",
        },
    )
    assert bad_reference.status_code == 422

    reference = client.post(
        "/api/v1/content-references",
        json={
            "ref_type": "content_item", "title": "自有作品抽象观察",
            "license_scope": "own_account", "usage_basis": "自有账号复盘与风格提炼",
            "reference_url": "https://example.invalid/v1",
            "note": "自制图形，字幕只留短句",
            "evidence_ids": [evidence["id"]],
            "observed_at": "2026-11-01T00:30:00Z",
            "material_statement": "no_third_party_media_or_verbatim_transcript",
        },
    ).json()
    assert reference["material_statement"] == "no_third_party_media_or_verbatim_transcript"

    # Workflow extraction builds an evidence-backed draft/pending card; human approval is required.
    card = service.create_workflow_style_card(
        title="证据提炼风格卡", book_id=book["id"],
        evidence_ids=[evidence["id"]], reference_ids=[reference["id"]],
    )
    assert card["derived_by"] == "workflow_extraction"
    assert card["review_status"] in {"draft", "pending_review"}
    assert card["features"], "evidence-backed features must be extracted"
    assert card["abstract_only"] is True

    # Reference-only patterns must not become evidence-backed features.
    assert all(feature["evidence_ids"] for feature in card["features"])

    approved = client.post(
        f"/api/v1/style-cards/{card['id']}/review",
        json={"decision": "approve", "note": "证据充分，特征为抽象模式"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["review_status"] == "approved"

    # Project brief so the generation snapshot can carry approved abstract guidance.
    project = client.post(
        "/api/v1/creative-projects",
        json={
            "book_id": book["id"],
            "promotion_candidate_id": candidate["id"],
            "target_audience": "喜欢个人规划的成年读者",
            "target_platform": "douyin",
            "desired_seconds": 30,
            "original_angle": "用自制地图图形表达一次小选择，邀请观众分享方法。",
            "prohibited_claims": ["真实销量", "疗效", "保证改变人生"],
            "evidence_ids": [evidence["id"]],
        },
    ).json()

    snapshot = service.get_generation_snapshot(project["id"])
    guidance = {item["feature_key"] for item in snapshot["style_guidance"]}
    assert guidance, "approved style card guidance must reach the generation snapshot"
    # Guidance carries only abstract keys/labels/counts, never note wording.
    for item in snapshot["style_guidance"]:
        assert set(item) == {"feature_key", "label", "sample_count"}
    assert "开场用提问" not in json.dumps(snapshot["style_guidance"], ensure_ascii=False)

    # CSV import: preview never writes; confirm partially applies with row diagnostics.
    csv_bytes = FIXTURE_CSV.read_bytes()
    preview = client.post(
        "/api/v1/imports/preview",
        files={"file": ("research-import-fixture.csv", csv_bytes, "text/csv")},
        headers={"Idempotency-Key": "w4-integ-preview-0001"},
    ).json()
    assert preview["total_rows"] == 6
    assert preview["encoding_detected"] == "utf8_bom"
    diagnostic_codes = {(d["row_number"], d["code"]) for d in preview["row_diagnostics"]}
    assert (2, "invalid_number") in diagnostic_codes
    assert (3, "formula_injection") in diagnostic_codes
    assert (4, "duplicate_natural_key") in diagnostic_codes
    assert (5, "edition_mismatch_suspected") in diagnostic_codes
    assert (6, "invalid_timestamp") in diagnostic_codes

    confirm = client.post(
        f"/api/v1/imports/{preview['id']}/confirm",
        headers={"Idempotency-Key": "w4-integ-confirm-0001"},
        json={"mapping": preview["mapping_suggestions"], "on_duplicate": "skip",
              "match_by": "isbn_and_edition", "normalize_isbn": True, "note": None},
    )
    assert confirm.status_code == 200, confirm.text
    job = confirm.json()
    assert job["status"] == "partially_applied"
    assert job["counts"]["failed"] == 3
    assert job["counts"]["skipped_duplicate"] >= 1
    assert "metric_observation" in job["entity_kinds"]

    # Replaying the preview with the same key returns the same draft.
    replay_preview = client.post(
        "/api/v1/imports/preview",
        files={"file": ("research-import-fixture.csv", csv_bytes, "text/csv")},
        headers={"Idempotency-Key": "w4-integ-preview-0001"},
    )
    assert replay_preview.status_code == 200
    assert replay_preview.json()["id"] == preview["id"]

    # A second confirm on an applied job is rejected; the first confirm is not duplicated.
    second_confirm = client.post(
        f"/api/v1/imports/{preview['id']}/confirm",
        headers={"Idempotency-Key": "w4-integ-confirm-0002"},
        json={"mapping": preview["mapping_suggestions"]},
    )
    assert second_confirm.status_code == 409
    assert second_confirm.json()["code"] == "import_already_applied"


def test_w4_records_are_owner_scoped(tmp_path: Path) -> None:
    client, service = _app(tmp_path)
    other = DomainService(service.database, owner_user_id=OTHER_ID, render_output_dir=service.render_output_dir)

    evidence = client.post("/api/v1/evidence", json=_evidence_body()).json()
    metric = client.post("/api/v1/metrics", json=_metric_body(evidence["id"])).json()
    reference = client.post(
        "/api/v1/content-references",
        json={
            "ref_type": "content_item", "title": "自有作品观察",
            "license_scope": "own_account", "usage_basis": "复盘",
            "evidence_ids": [evidence["id"]],
            "material_statement": "no_third_party_media_or_verbatim_transcript",
        },
    ).json()
    card = service.create_workflow_style_card(evidence_ids=[evidence["id"]], reference_ids=[reference["id"]])

    preview_csv = "isbn,title,edition_label,author\n978-0-0000-0001-1,虚构书,演示版 1.0,作者\n".encode("utf-8")
    preview = client.post(
        "/api/v1/imports/preview",
        files={"file": ("fictitious.csv", preview_csv, "text/csv")},
        headers={"Idempotency-Key": "w4-owner-preview-0001"},
    ).json()

    for action, resource_id in (
        (other.get_metric_observation, metric["id"]),
        (other.get_content_reference, reference["id"]),
        (other.get_style_card, card["id"]),
        (other.get_import, preview["id"]),
    ):
        try:
            action(resource_id)
        except Exception as error:
            assert getattr(error, "status_code", None) == 404, getattr(error, "code", None)
        else:
            raise AssertionError("cross-owner read must be rejected with 404")

    # The other owner sees an empty research list.
    assert other.list_metric_observations()["items"] == []
    assert other.list_content_references()["items"] == []
    assert other.list_style_cards()["items"] == []
    assert other.list_imports()["items"] == []
