from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from book_video.media.ffmpeg import MediaDependencyError, render_mp4
from book_video.tasks.render import (
    IdempotencyConflictError,
    InMemoryIdempotencyStore,
    RenderSnapshotError,
    run_render_task,
)


PROJECT_ID = "20000000-0000-4000-8000-000000000001"
BOOK_ID = "30000000-0000-4000-8000-000000000001"
CANDIDATE_ID = "40000000-0000-4000-8000-000000000001"
SCRIPT_ID = "60000000-0000-4000-8000-000000000001"
OWNER_ID = "50000000-0000-4000-8000-000000000001"


def render_snapshot(*, review_status: str = "approved", eligibility: str = "manually_confirmed") -> dict[str, Any]:
    return {
        "project": {
            "id": PROJECT_ID,
            "owner_user_id": OWNER_ID,
            "book_id": BOOK_ID,
            "promotion_candidate_id": CANDIDATE_ID,
            "target_platform": "douyin",
            "desired_seconds": 30,
        },
        "book": {
            "id": BOOK_ID,
            "facts": [
                {"key": "title", "value": "虚构样书", "verification_status": "human_confirmed"}
            ],
        },
        "promotion_candidate": {
            "id": CANDIDATE_ID,
            "book_id": BOOK_ID,
            "platform": "douyin",
            "eligibility_status": eligibility,
        },
        "script_version": {
            "id": SCRIPT_ID,
            "project_id": PROJECT_ID,
            "review_status": review_status,
            "segments": [
                {"order": 0, "onscreen_text": "第一段", "fact_keys": [], "duration_seconds": 6.0},
                {"order": 1, "onscreen_text": "已核对：虚构样书", "fact_keys": ["title"], "duration_seconds": 7.0},
                {"order": 2, "onscreen_text": "我的原创观点", "fact_keys": [], "duration_seconds": 11.0},
                {"order": 3, "onscreen_text": "欢迎交流", "fact_keys": [], "duration_seconds": 6.0},
            ],
        },
        "width": 1080,
        "height": 1920,
        "asset_rights": [
            {"origin": "self_created_text", "usage_status": "allowed", "license_basis": "本地生成文字"},
            {"origin": "self_created_graphic", "usage_status": "allowed", "license_basis": "本地绘制图形"},
        ],
    }


def test_render_task_reuses_same_result_for_idempotency_key(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    store = InMemoryIdempotencyStore()

    def fake_renderer(segments: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"segments": deepcopy(segments), **kwargs})
        return {
            "path": str(kwargs["output_path"]),
            "container": "mp4",
            "video_codec": "h264",
            "width": 1080,
            "height": 1920,
            "duration_seconds": 30.0,
            "audio_present": False,
        }

    first = run_render_task(
        render_snapshot(),
        idempotency_key="demo-render-key-001",
        output_dir=tmp_path,
        idempotency_store=store,
        renderer=fake_renderer,
    )
    second = run_render_task(
        render_snapshot(),
        idempotency_key="demo-render-key-001",
        output_dir=tmp_path,
        idempotency_store=store,
        renderer=fake_renderer,
    )

    assert first == second
    assert first["status"] == "succeeded"
    assert first["output_ref"].endswith(".mp4")
    assert len(calls) == 1


def test_idempotency_key_cannot_be_reused_for_a_different_request(tmp_path: Path) -> None:
    store = InMemoryIdempotencyStore()
    fake_renderer = lambda *_args, **_kwargs: {"duration_seconds": 30.0}
    run_render_task(
        render_snapshot(),
        idempotency_key="demo-render-key-002",
        output_dir=tmp_path,
        idempotency_store=store,
        renderer=fake_renderer,
    )
    changed = render_snapshot()
    changed["script_version"]["segments"][0]["onscreen_text"] = "内容已变化"
    with pytest.raises(IdempotencyConflictError):
        run_render_task(
            changed,
            idempotency_key="demo-render-key-002",
            output_dir=tmp_path,
            idempotency_store=store,
            renderer=fake_renderer,
        )


def test_render_is_blocked_before_approval_or_without_manual_product_check(tmp_path: Path) -> None:
    with pytest.raises(RenderSnapshotError, match="approved before rendering"):
        run_render_task(
            render_snapshot(review_status="pending"),
            idempotency_key="demo-render-key-003",
            output_dir=tmp_path,
            idempotency_store=InMemoryIdempotencyStore(),
            renderer=lambda *_args, **_kwargs: {},
        )
    with pytest.raises(RenderSnapshotError, match="manually confirmed"):
        run_render_task(
            render_snapshot(eligibility="unverified"),
            idempotency_key="demo-render-key-004",
            output_dir=tmp_path,
            idempotency_store=InMemoryIdempotencyStore(),
            renderer=lambda *_args, **_kwargs: {},
        )


def test_render_rights_reject_blocked_and_expired_assets(tmp_path: Path) -> None:
    snapshot = render_snapshot()
    snapshot["asset_rights"].append(
        {"origin": "licensed", "usage_status": "blocked", "license_basis": "demo", "proof_ref": "demo", "permitted_uses": ["render"]}
    )
    with pytest.raises(RenderSnapshotError, match="asset rights check failed"):
        run_render_task(
            snapshot,
            idempotency_key="demo-render-key-005",
            output_dir=tmp_path,
            idempotency_store=InMemoryIdempotencyStore(),
            renderer=lambda *_args, **_kwargs: {},
        )

    snapshot["asset_rights"] = snapshot["asset_rights"][:2] + [
        {
            "origin": "licensed",
            "usage_status": "allowed",
            "license_basis": "虚构演示许可",
            "proof_ref": "demo-only",
            "permitted_uses": ["render"],
            "expires_at": "2000-01-01T00:00:00Z",
        }
    ]
    with pytest.raises(RenderSnapshotError, match="asset rights check failed"):
        run_render_task(
            snapshot,
            idempotency_key="demo-render-key-006",
            output_dir=tmp_path,
            idempotency_store=InMemoryIdempotencyStore(),
            renderer=lambda *_args, **_kwargs: {},
        )


def test_render_requires_allowed_rights_for_its_generated_assets(tmp_path: Path) -> None:
    snapshot = render_snapshot()
    snapshot["asset_rights"] = []
    with pytest.raises(RenderSnapshotError, match="asset_rights_missing:self_created_text"):
        run_render_task(
            snapshot,
            idempotency_key="demo-render-key-007",
            output_dir=tmp_path,
            idempotency_store=InMemoryIdempotencyStore(),
            renderer=lambda *_args, **_kwargs: {},
        )


def test_local_renderer_uses_generated_graphics_and_emits_silent_mp4(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from book_video.media import ffmpeg as ffmpeg_module

    font = tmp_path / "fake-font.ttc"
    font.write_bytes(b"test font placeholder")
    monkeypatch.setattr(ffmpeg_module, "_find_font", lambda _font_path=None: font)
    commands: list[list[str]] = []

    def fake_subprocess_run(command: list[str], **_kwargs: Any) -> Any:
        commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"fake mp4 bytes")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920}
                    ],
                    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "30.0"},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(ffmpeg_module.subprocess, "run", fake_subprocess_run)
    output = tmp_path / "render.mp4"
    metadata = render_mp4(
        render_snapshot()["script_version"]["segments"],
        duration_seconds=30,
        output_path=output,
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        font_path=font,
    )

    ffmpeg_command = commands[0]
    assert "-an" in ffmpeg_command
    assert "libx264" in ffmpeg_command
    assert ffmpeg_command[ffmpeg_command.index("-i") + 1].startswith("color=")
    assert metadata["width"] == 1080
    assert metadata["height"] == 1920
    assert metadata["audio_present"] is False
    assert output.is_file()


def test_render_mp4_reports_missing_ffmpeg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("book_video.media.ffmpeg.shutil.which", lambda _name: None)
    with pytest.raises(MediaDependencyError, match="ffmpeg is required"):
        render_mp4(
            [{"order": 0, "onscreen_text": "演示", "duration_seconds": 30}],
            duration_seconds=30,
            output_path=tmp_path / "demo.mp4",
        )
