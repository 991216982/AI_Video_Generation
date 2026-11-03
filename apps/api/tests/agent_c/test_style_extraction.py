"""W4-C: evidence-backed abstract style extraction and generation passthrough."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from book_video.workflows.script_generation import run_script_generation
from book_video.workflows.style_extraction import (
    extract_style_features,
    style_guidance,
)

RUN_ID = "10000000-0000-4000-8000-00000000000C"
PROJECT_ID = "20000000-0000-4000-8000-00000000000C"
BOOK_ID = "30000000-0000-4000-8000-00000000000C"
CANDIDATE_ID = "40000000-0000-4000-8000-00000000000C"
OWNER_ID = "50000000-0000-4000-8000-00000000000C"
EVIDENCE_ID = "60000000-0000-4000-8000-000000000001"
REFERENCE_ID = "60000000-0000-4000-8000-000000000002"


def _evidence() -> dict[str, Any]:
    return {
        "id": EVIDENCE_ID,
        "kind": "evidence",
        "note": "开场用提问吸引停留；语气像朋友推荐；结尾邀请观众分享自己的方法。",
        "source_note": "创作者人工观察记录",
        "title": "自有作品观察",
    }


def _reference() -> dict[str, Any]:
    return {
        "id": REFERENCE_ID,
        "kind": "reference",
        "note": "画面用自制图形，字幕只留短句；节奏平稳。",
        "title": "同类作品抽象观察",
    }


def test_extraction_builds_evidence_backed_abstract_features() -> None:
    features, evidence_ids, reference_ids = extract_style_features([_evidence(), _reference()])

    by_key = {feature["feature_key"]: feature for feature in features}
    assert evidence_ids == [EVIDENCE_ID]
    assert reference_ids == [REFERENCE_ID]

    opening = by_key["opening_hook"]
    assert opening["evidence_ids"] == [EVIDENCE_ID]
    assert opening["sample_count"] == 1
    assert opening["label"] and opening["explanation"]

    # Reference-only observations do not form a feature without evidence backing;
    # the reference is still linked at card level for traceability.
    assert "visual_style" not in by_key
    assert "pacing" not in by_key

    # No third-party wording or transcript is copied into the feature definition.
    for feature in features:
        assert "开场用提问" not in feature["explanation"]
        assert isinstance(feature["explanation"], str) and len(feature["explanation"]) <= 500


def test_extraction_without_evidence_yields_no_evidence_features() -> None:
    features, evidence_ids, reference_ids = extract_style_features([_reference()])
    assert evidence_ids == []
    assert reference_ids == [REFERENCE_ID]
    assert all(feature["evidence_ids"] == [] for feature in features)


def test_style_guidance_is_abstract_and_omits_source_text() -> None:
    features, _, _ = extract_style_features([_evidence()])
    guidance = style_guidance(features)
    assert {item["feature_key"] for item in guidance} == {feature["feature_key"] for feature in features}
    for item in guidance:
        assert set(item.keys()) == {"feature_key", "label", "sample_count"}
        assert "开场用提问" not in item["label"]
    altered = [{**features[0], "label": "不得复制的第三方原话"}]
    assert "不得复制的第三方原话" not in str(style_guidance(altered))


def _snapshot(*, with_guidance: bool) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "run": {"id": RUN_ID, "sequence": 1},
        "project": {
            "id": PROJECT_ID,
            "owner_user_id": OWNER_ID,
            "book_id": BOOK_ID,
            "promotion_candidate_id": CANDIDATE_ID,
            "target_audience": "喜欢个人规划的成年读者",
            "target_platform": "douyin",
            "desired_seconds": 30,
            "original_angle": "用自制地图图形表达一次小选择。",
            "prohibited_claims": ["保证改变人生", "真实销量"],
        },
        "book": {
            "id": BOOK_ID,
            "title": "把日子写成地图",
            "edition_label": "虚构演示版 1.0",
            "author": "林例",
            "original_opinion": "我喜欢用地图比喻日常选择；这本虚构样书仅用于演示原创观点。",
            "facts": [
                {
                    "key": "title",
                    "value": "把日子写成地图",
                    "verification_status": "human_confirmed",
                    "source_note": "虚构演示数据，已由创作者人工核对",
                    "revision": 1,
                }
            ],
        },
        "promotion_candidate": {
            "id": CANDIDATE_ID,
            "book_id": BOOK_ID,
            "platform": "douyin",
            "platform_product_id": "DEMO-NOT-REAL",
            "eligibility_status": "manually_confirmed",
        },
        "asset_rights": [
            {"origin": "self_created_text", "usage_status": "allowed"},
            {"origin": "self_created_graphic", "usage_status": "allowed"},
        ],
    }
    if with_guidance:
        snapshot["style_guidance"] = style_guidance(extract_style_features([_evidence()])[0])
    return snapshot


def test_approved_abstract_patterns_make_original_segments_observably_different() -> None:
    from book_video.workflows.script_generation import _make_scripts

    opening = _snapshot(with_guidance=False)
    opening["style_guidance"] = [{
        "feature_key": "opening_hook", "label": "仅供演示的抽象开场", "sample_count": 1,
    }]
    tone = _snapshot(with_guidance=False)
    tone["style_guidance"] = [{
        "feature_key": "tone", "label": "仅供演示的抽象语气", "sample_count": 1,
    }]
    opening_script = _make_scripts(opening, RUN_ID)[0]
    tone_script = _make_scripts(tone, RUN_ID)[0]

    assert opening_script["segments"][0]["narration"] != tone_script["segments"][0]["narration"]
    assert opening_script["segments"][2]["narration"] != tone_script["segments"][2]["narration"]
    for script in (opening_script, tone_script):
        text = " ".join(str(segment["narration"]) for segment in script["segments"])
        assert opening["book"]["original_opinion"] in text
        assert "仅供演示的抽象" not in text
        assert any(segment["fact_keys"] == ["title"] for segment in script["segments"])


def test_generation_accepts_approved_style_guidance_and_keeps_scripts_original() -> None:
    from book_video.workflows.model_adapter import ModelAdapterError

    captured_context: dict[str, Any] = {}

    class RecordingAdapter:
        def generate(self, *, variant: str, context: dict[str, Any]) -> Any:
            captured_context[variant] = deepcopy(context)
            raise ModelAdapterError("invalid_output", "recording adapter does not produce content")

    checkpointer = MemorySaver()
    persisted: dict[str, dict[str, Any]] = {}
    paused = run_script_generation(
        _snapshot(with_guidance=True),
        run_id=RUN_ID,
        checkpointer=checkpointer,
        model_adapter=RecordingAdapter(),
        persist_script=lambda script: persisted.update({str(script["id"]): deepcopy(dict(script))}),
    )
    guidance = captured_context["A"]["style_guidance"]
    assert guidance, "approved style guidance must reach the generation context"
    assert guidance[0]["feature_key"] in {item["feature_key"] for item in guidance}
    # Style hints are abstract; no source note wording appears in generated scripts.
    for script in paused["scripts"]:
        for segment in script["segments"]:
            assert "开场用提问" not in segment["onscreen_text"]
            assert "开场用提问" not in segment["narration"]

    selected_id = paused["scripts"][0]["id"]
    completed = run_script_generation(
        None,
        run_id=RUN_ID,
        checkpointer=checkpointer,
        persist_script=lambda script: persisted.update({str(script["id"]): deepcopy(dict(script))}),
        review_decision={
            "script_version_id": selected_id,
            "decision": "approve",
            "fact_confirmed": True,
            "product_match_confirmed": True,
            "asset_rights_confirmed": True,
        },
    )
    assert completed["status"] == "succeeded"
