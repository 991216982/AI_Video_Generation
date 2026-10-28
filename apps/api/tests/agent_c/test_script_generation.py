from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from book_video.workflows.script_generation import (
    ReviewGateError,
    ScriptGenerationError,
    run_script_generation,
)


RUN_ID = "10000000-0000-4000-8000-000000000001"
PROJECT_ID = "20000000-0000-4000-8000-000000000001"
BOOK_ID = "30000000-0000-4000-8000-000000000001"
CANDIDATE_ID = "40000000-0000-4000-8000-000000000001"
OWNER_ID = "50000000-0000-4000-8000-000000000001"


def generation_snapshot(*, eligibility: str = "manually_confirmed") -> dict[str, Any]:
    return {
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
                },
                {
                    "key": "unverified_note",
                    "value": "不得进入成片",
                    "verification_status": "unverified",
                },
            ],
        },
        "promotion_candidate": {
            "id": CANDIDATE_ID,
            "book_id": BOOK_ID,
            "platform": "douyin",
            "platform_product_id": "DEMO-NOT-REAL",
            "eligibility_status": eligibility,
        },
        "asset_rights": [
            {"origin": "self_created_text", "usage_status": "allowed", "license_basis": "本地生成文字"},
            {"origin": "self_created_graphic", "usage_status": "allowed", "license_basis": "本地绘制图形"},
        ],
    }


def test_script_generation_pauses_for_review_and_resumes_with_approval() -> None:
    checkpointer = MemorySaver()
    emitted_events: list[dict[str, Any]] = []
    persisted: dict[str, dict[str, Any]] = {}

    def emit(event: Any) -> None:
        emitted_events.append(deepcopy(dict(event)))

    def persist(script: Any) -> None:
        persisted[str(script["id"])] = deepcopy(dict(script))

    paused = run_script_generation(
        generation_snapshot(),
        run_id=RUN_ID,
        checkpointer=checkpointer,
        emit_event=emit,
        persist_script=persist,
    )

    assert paused["status"] == "waiting_review"
    assert paused["waiting_for_review"] is True
    assert len(paused["scripts"]) == 2
    assert paused["scripts"][0]["id"] != paused["scripts"][1]["id"]
    assert all(item["review_status"] == "pending" for item in paused["scripts"])
    assert all(
        key == "title"
        for script in paused["scripts"]
        for segment in script["segments"]
        for key in segment["fact_keys"]
    )
    assert not any("不得进入成片" in segment["onscreen_text"] for script in paused["scripts"] for segment in script["segments"])
    assert {event["type"] for event in emitted_events} >= {
        "run.stage_changed",
        "run.review_requested",
    }

    selected_id = paused["scripts"][0]["id"]
    completed = run_script_generation(
        None,
        run_id=RUN_ID,
        checkpointer=checkpointer,
        emit_event=emit,
        persist_script=persist,
        review_decision={
            "script_version_id": selected_id,
            "decision": "approve",
            "fact_confirmed": True,
            "product_match_confirmed": True,
            "asset_rights_confirmed": True,
        },
    )

    assert completed["status"] == "succeeded"
    assert completed["waiting_for_review"] is False
    assert completed["approved_script_version_id"] == selected_id
    assert persisted[selected_id]["review_status"] == "approved"
    assert emitted_events[-1]["type"] == "run.completed"
    assert emitted_events[-1]["status"] == "succeeded"


def test_review_approval_is_blocked_until_candidate_is_manually_confirmed() -> None:
    checkpointer = MemorySaver()
    events: list[dict[str, Any]] = []
    snapshot = generation_snapshot(eligibility="unverified")
    paused = run_script_generation(
        snapshot,
        run_id=RUN_ID,
        checkpointer=checkpointer,
        emit_event=events.append,
    )

    resumed = run_script_generation(
        None,
        run_id=RUN_ID,
        checkpointer=checkpointer,
        emit_event=events.append,
        review_decision={
            "script_version_id": paused["scripts"][0]["id"],
            "decision": "approve",
            "fact_confirmed": True,
            "product_match_confirmed": True,
            "asset_rights_confirmed": True,
        },
    )

    assert resumed["status"] == "waiting_review"
    assert resumed["waiting_for_review"] is True
    assert "promotion_candidate_unverified" in resumed["review_gate_error"]
    assert "run.completed" not in {event["type"] for event in events}


def test_script_generation_rejects_snapshots_without_verified_metadata_facts() -> None:
    snapshot = generation_snapshot()
    snapshot["book"]["facts"][0]["verification_status"] = "unverified"
    with pytest.raises(ScriptGenerationError, match="verified bibliographic fact"):
        run_script_generation(snapshot, run_id=RUN_ID, checkpointer=MemorySaver())


def test_script_versions_follow_project_max_and_replay_keeps_assignments() -> None:
    snapshot = generation_snapshot()
    snapshot["script_versions"] = [
        {"id": "70000000-0000-4000-8000-000000000001", "version_number": 3},
        {"id": "70000000-0000-4000-8000-000000000002", "version_number": 12},
    ]
    first = run_script_generation(snapshot, run_id=RUN_ID, checkpointer=MemorySaver())
    versions = {script["id"]: script["version_number"] for script in first["scripts"]}
    assert sorted(versions.values()) == [13, 14]

    replay_snapshot = generation_snapshot()
    replay_snapshot["script_versions"] = snapshot["script_versions"] + first["scripts"]
    replay = run_script_generation(replay_snapshot, run_id=RUN_ID, checkpointer=MemorySaver())
    replay_versions = {script["id"]: script["version_number"] for script in replay["scripts"]}
    assert replay_versions == versions


def test_review_accepts_saved_edit_derived_from_this_run_and_rechecks_content() -> None:
    checkpointer = MemorySaver()
    persisted: dict[str, dict[str, Any]] = {}

    def persist(script: Any) -> None:
        persisted[str(script["id"])] = deepcopy(dict(script))

    paused = run_script_generation(
        generation_snapshot(),
        run_id=RUN_ID,
        checkpointer=checkpointer,
        persist_script=persist,
    )
    base = paused["scripts"][1]
    edited = deepcopy(base)
    edited.update(
        {
            "id": "80000000-0000-4000-8000-000000000001",
            "version_number": 3,
            "based_on_version_id": base["id"],
            # A's review_run may persist approved status before the worker resumes.
            "review_status": "approved",
        }
    )
    edited["segments"][0]["onscreen_text"] = "从一个具体观察开始"

    completed = run_script_generation(
        None,
        run_id=RUN_ID,
        checkpointer=checkpointer,
        persist_script=persist,
        review_decision={
            "script_version_id": edited["id"],
            "script_version": edited,
            "decision": "approve",
            "fact_confirmed": True,
            "product_match_confirmed": True,
            "asset_rights_confirmed": True,
        },
    )

    assert completed["status"] == "succeeded"
    assert completed["approved_script_version_id"] == edited["id"]
    assert persisted[edited["id"]]["review_status"] == "approved"
    assert any(script["id"] == edited["id"] for script in completed["scripts"])


def test_review_blocks_edits_with_unverified_facts_and_rejects_unrelated_base() -> None:
    checkpointer = MemorySaver()
    paused = run_script_generation(
        generation_snapshot(),
        run_id=RUN_ID,
        checkpointer=checkpointer,
    )
    base = paused["scripts"][0]
    edited = deepcopy(base)
    edited.update(
        {
            "id": "80000000-0000-4000-8000-000000000002",
            "version_number": 3,
            "based_on_version_id": base["id"],
            "review_status": "pending",
        }
    )
    edited["segments"][1]["fact_keys"] = ["unverified_note"]

    blocked = run_script_generation(
        None,
        run_id=RUN_ID,
        checkpointer=checkpointer,
        review_decision={
            "script_version_id": edited["id"],
            "script_version": edited,
            "decision": "approve",
            "fact_confirmed": True,
            "product_match_confirmed": True,
            "asset_rights_confirmed": True,
        },
    )
    assert blocked["status"] == "waiting_review"
    assert "script_facts_not_verified" in blocked["review_gate_error"]

    unrelated = deepcopy(edited)
    unrelated["id"] = "80000000-0000-4000-8000-000000000003"
    unrelated["based_on_version_id"] = "90000000-0000-4000-8000-000000000001"
    with pytest.raises(ReviewGateError, match="based on an original version"):
        run_script_generation(
            None,
            run_id=RUN_ID,
            checkpointer=checkpointer,
            review_decision={
                "script_version_id": unrelated["id"],
                "script_version": unrelated,
                "decision": "approve",
                "fact_confirmed": True,
                "product_match_confirmed": True,
                "asset_rights_confirmed": True,
            },
        )
