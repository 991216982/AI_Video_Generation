"""Synthetic audio is used only to exercise private voice-file muxing."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import wave

import pytest

from book_video.media.ffmpeg import probe_mp4
from book_video.tasks.render import InMemoryIdempotencyStore, RenderSnapshotError, run_render_task

from test_render_task import OWNER_ID, render_snapshot


def _test_audio(path: Path) -> Path:
    # Invented silent PCM fixture; it is not a real person's recording.
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000 * 20)
    return path


def _audio_snapshot() -> dict:
    snapshot = render_snapshot()
    snapshot["project"]["desired_seconds"] = 20
    for segment, duration in zip(snapshot["script_version"]["segments"], (4.0, 4.4, 7.6, 4.0)):
        segment["duration_seconds"] = duration
    snapshot["audio_asset_id"] = "90000000-0000-4000-8000-000000000001"
    snapshot["audio_asset"] = {
        "id": snapshot["audio_asset_id"], "owner_user_id": OWNER_ID,
        "origin": "self_recorded_voice", "usage_status": "allowed",
        "rights_attested": True, "rights_proof_note": "虚构演示：合成测试音频，不是现实录音",
        "permitted_uses": "render_and_manual_publish", "expires_at": None,
    }
    return snapshot


def test_ffprobe_confirms_real_w3_audio_and_silent_outputs(tmp_path: Path) -> None:
    audio_path = _test_audio(tmp_path / "invented-test-audio.wav")
    snapshot = _audio_snapshot()
    calls: list[str] = []

    def resolve(asset_id: str) -> Path:
        calls.append(asset_id)
        return audio_path

    voiced = run_render_task(
        snapshot, idempotency_key="w3-voice-demo-001", output_dir=tmp_path,
        idempotency_store=InMemoryIdempotencyStore(), audio_resolver=resolve,
    )
    metadata = probe_mp4(voiced["output_ref"])
    assert voiced["status"] == "succeeded"
    assert metadata["width"] == 1080 and metadata["height"] == 1920
    assert 20 <= metadata["duration_seconds"] <= 45
    assert metadata["audio_present"] is True
    assert calls == [snapshot["audio_asset_id"]]

    silent = render_snapshot()
    silent["project"]["desired_seconds"] = 20
    for segment, duration in zip(silent["script_version"]["segments"], (4.0, 4.4, 7.6, 4.0)):
        segment["duration_seconds"] = duration
    silent_job = run_render_task(
        silent, idempotency_key="w3-silent-demo-001", output_dir=tmp_path,
        idempotency_store=InMemoryIdempotencyStore(), audio_resolver=resolve,
    )
    silent_metadata = probe_mp4(silent_job["output_ref"])
    assert silent_job["status"] == "succeeded"
    assert silent_metadata["width"] == 1080 and silent_metadata["height"] == 1920
    assert 20 <= silent_metadata["duration_seconds"] <= 45
    assert silent_metadata["audio_present"] is False
    assert calls == [snapshot["audio_asset_id"]]


def test_audio_requires_server_resolution_valid_owner_and_current_rights(tmp_path: Path) -> None:
    snapshot = _audio_snapshot()
    with pytest.raises(RenderSnapshotError, match="server media resolver"):
        run_render_task(snapshot, idempotency_key="w3-audio-no-resolver", output_dir=tmp_path)
    wrong_owner = deepcopy(snapshot)
    wrong_owner["audio_asset"]["owner_user_id"] = "other-owner"
    with pytest.raises(RenderSnapshotError, match="owner or ID mismatch"):
        run_render_task(wrong_owner, idempotency_key="w3-audio-wrong-owner", output_dir=tmp_path)
    expired = deepcopy(snapshot)
    expired["audio_asset"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with pytest.raises(RenderSnapshotError, match="expired or invalid"):
        run_render_task(expired, idempotency_key="w3-audio-expired", output_dir=tmp_path)
    blocked = deepcopy(snapshot)
    blocked["audio_asset"]["rights_proof_note"] = ""
    with pytest.raises(RenderSnapshotError, match="proof is incomplete"):
        run_render_task(blocked, idempotency_key="w3-audio-no-proof", output_dir=tmp_path)
    injected_path = deepcopy(snapshot)
    injected_path["audio_media_path"] = str(tmp_path / "client.wav")
    with pytest.raises(RenderSnapshotError, match="client media paths"):
        run_render_task(injected_path, idempotency_key="w3-audio-path-injection", output_dir=tmp_path)


def test_licensed_voice_origin_is_preserved_in_render_metadata(tmp_path: Path) -> None:
    audio_path = _test_audio(tmp_path / "invented-licensed-fixture.wav")
    snapshot = _audio_snapshot()
    snapshot["audio_asset"]["origin"] = "licensed_voice"
    snapshot["audio_asset"]["rights_proof_note"] = "虚构演示许可：仅用于本地测试"
    snapshot["audio_asset"]["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    result = run_render_task(
        snapshot, idempotency_key="w3-licensed-demo-001", output_dir=tmp_path,
        idempotency_store=InMemoryIdempotencyStore(), audio_resolver=lambda _asset_id: audio_path,
    )
    assert result["status"] == "succeeded"
    assert result["media"]["asset_origins"] == [
        "self_created_text", "self_created_graphic", "licensed_voice",
    ]
    assert probe_mp4(result["output_ref"])["audio_present"] is True
