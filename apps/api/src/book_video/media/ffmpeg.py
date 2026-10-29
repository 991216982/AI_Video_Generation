"""FFmpeg renderer for silent, text-first 9:16 video and ffprobe checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence


WIDTH = 1080
HEIGHT = 1920
MIN_DURATION_SECONDS = 20
MAX_DURATION_SECONDS = 45
FRAME_RATE = 30


class MediaDependencyError(RuntimeError):
    """FFmpeg, ffprobe, or a usable CJK font is missing."""


class MediaRenderError(RuntimeError):
    """FFmpeg failed or generated media did not satisfy the quality gate."""


class MediaCancelledError(RuntimeError):
    """The owning render received a persisted cancellation request."""


def _check_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise MediaCancelledError("render cancellation was requested")


def _run_ffmpeg(command: list[str], cancel_check: Callable[[], bool] | None) -> Any:
    if cancel_check is None:
        return subprocess.run(command, check=False, capture_output=True, text=True)

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while True:
        if cancel_check():
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise MediaCancelledError("render cancellation was requested")
        try:
            stdout, stderr = process.communicate(timeout=0.1)
            return SimpleNamespace(returncode=process.returncode, stdout=stdout, stderr=stderr)
        except subprocess.TimeoutExpired:
            continue


def _find_font(font_path: str | Path | None = None) -> Path:
    if font_path is not None:
        path = Path(font_path)
        if path.is_file():
            return path
        raise MediaDependencyError(f"CJK font file not found: {path}")

    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc",
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise MediaDependencyError("no CJK font found; install Noto Sans CJK or provide font_path")


def _filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    for character in ("\\", "'", ":", ",", "[", "]", ";"):
        value = value.replace(character, "\\" + character)
    return f"'{value}'"


def _wrap_text(value: str, width: int = 14) -> str:
    lines: list[str] = []
    for paragraph in value.splitlines() or [value]:
        if not paragraph:
            lines.append("")
            continue
        lines.extend(paragraph[index : index + width] for index in range(0, len(paragraph), width))
    return "\n".join(lines)


def _build_filter_graph(
    *,
    segments: Sequence[Mapping[str, Any]],
    text_paths: Sequence[Path],
    font_path: Path,
) -> str:
    filters = [
        "drawbox=x=48:y=210:w=984:h=1500:color=0x172554:t=fill",
        "drawbox=x=92:y=280:w=154:h=18:color=0x38bdf8:t=fill",
        "drawbox=x=92:y=1640:w=896:h=4:color=0x334155:t=fill",
    ]
    for segment, text_path in zip(segments, text_paths):
        start = sum(float(item["duration_seconds"]) for item in segments[: int(segment["order"])])
        end = start + float(segment["duration_seconds"])
        filters.append(
            "drawtext="
            f"fontfile={_filter_path(font_path)}:"
            f"textfile={_filter_path(text_path)}:"
            "fontcolor=white:fontsize=60:line_spacing=22:"
            "x=(w-text_w)/2:y=(h-text_h)/2:"
            f"enable='between(t,{start:.3f},{end:.3f})':fix_bounds=true"
        )
    return ",".join(filters)


def _ffprobe_binary(ffprobe_path: str | None = None) -> str:
    selected = ffprobe_path or shutil.which("ffprobe")
    if not selected:
        raise MediaDependencyError("ffprobe is required for MP4 quality checks")
    return selected


def probe_mp4(
    path: str | Path,
    *,
    ffprobe_path: str | None = None,
    expected_width: int = WIDTH,
    expected_height: int = HEIGHT,
    minimum_duration: int = MIN_DURATION_SECONDS,
    maximum_duration: int = MAX_DURATION_SECONDS,
) -> dict[str, Any]:
    """Read and validate an MP4's dimensions, duration, and video stream."""

    media_path = Path(path)
    if not media_path.is_file() or media_path.stat().st_size == 0:
        raise MediaRenderError(f"rendered MP4 is missing or empty: {media_path}")
    command = [
        _ffprobe_binary(ffprobe_path),
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height",
        "-show_entries",
        "format=format_name,duration",
        "-of",
        "json",
        str(media_path),
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        raise MediaRenderError(f"ffprobe failed: {completed.stderr.strip() or completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams", [])
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        duration = float(payload["format"]["duration"])
    except (KeyError, TypeError, ValueError, StopIteration, json.JSONDecodeError) as error:
        raise MediaRenderError("ffprobe returned incomplete MP4 metadata") from error

    formats = str(payload.get("format", {}).get("format_name", "")).split(",")
    if "mp4" not in formats and "mov" not in formats:
        raise MediaRenderError("render output is not an MP4 container")
    if int(video.get("width", 0)) != expected_width or int(video.get("height", 0)) != expected_height:
        raise MediaRenderError(
            f"expected {expected_width}x{expected_height} video, got "
            f"{video.get('width')}x{video.get('height')}"
        )
    if not minimum_duration <= duration <= maximum_duration:
        raise MediaRenderError(
            f"video duration {duration:.3f}s is outside {minimum_duration}-{maximum_duration}s"
        )
    return {
        "path": str(media_path),
        "container": "mp4",
        "video_codec": video.get("codec_name"),
        "width": expected_width,
        "height": expected_height,
        "duration_seconds": duration,
        "audio_present": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def render_mp4(
    segments: Sequence[Mapping[str, Any]],
    *,
    duration_seconds: int,
    output_path: str | Path,
    width: int = WIDTH,
    height: int = HEIGHT,
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
    font_path: str | Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Render only supplied script text plus generated geometric shapes.

    The output is always silent. No image, music, voice, TTS, network URL, or
    book-cover path is accepted by this function.
    """

    _check_cancelled(cancel_check)
    if width != WIDTH or height != HEIGHT:
        raise MediaRenderError(f"only {WIDTH}x{HEIGHT} output is supported")
    if not MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS:
        raise MediaRenderError(
            f"duration_seconds must be between {MIN_DURATION_SECONDS} and {MAX_DURATION_SECONDS}"
        )
    if not segments:
        raise MediaRenderError("at least one script segment is required")
    durations = [float(segment.get("duration_seconds", 0)) for segment in segments]
    if any(value <= 0 for value in durations) or abs(sum(durations) - duration_seconds) > 0.05:
        raise MediaRenderError("segment durations must be positive and sum to duration_seconds")
    if any(not str(segment.get("onscreen_text", "")).strip() for segment in segments):
        raise MediaRenderError("each segment must include onscreen_text")

    ffmpeg_binary = ffmpeg_path or shutil.which("ffmpeg")
    if not ffmpeg_binary:
        raise MediaDependencyError("ffmpeg is required to render MP4")
    selected_font = _find_font(font_path)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="book-video-render-", dir=str(destination.parent)) as work_dir:
        work = Path(work_dir)
        text_paths: list[Path] = []
        for index, segment in enumerate(segments):
            text_path = work / f"segment-{index:02d}.txt"
            text_path.write_text(_wrap_text(str(segment["onscreen_text"])), encoding="utf-8")
            text_paths.append(text_path)
        temporary_output = work / "render.mp4"
        filter_graph = _build_filter_graph(segments=segments, text_paths=text_paths, font_path=selected_font)
        command = [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x0b1020:s={width}x{height}:r={FRAME_RATE}:d={duration_seconds}",
            "-vf",
            filter_graph,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-t",
            str(duration_seconds),
            str(temporary_output),
        ]
        completed = _run_ffmpeg(command, cancel_check)
        if completed.returncode != 0:
            raise MediaRenderError(f"ffmpeg failed: {completed.stderr.strip() or completed.returncode}")
        if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            raise MediaRenderError("ffmpeg completed without creating an MP4")
        _check_cancelled(cancel_check)
        os.replace(temporary_output, destination)

    try:
        _check_cancelled(cancel_check)
    except MediaCancelledError:
        destination.unlink(missing_ok=True)
        raise

    try:
        metadata = probe_mp4(
            destination,
            ffprobe_path=ffprobe_path,
            expected_width=width,
            expected_height=height,
            minimum_duration=MIN_DURATION_SECONDS,
            maximum_duration=MAX_DURATION_SECONDS,
        )
        _check_cancelled(cancel_check)
    except MediaCancelledError:
        destination.unlink(missing_ok=True)
        raise
    if metadata["audio_present"]:
        raise MediaRenderError("output unexpectedly contains audio; silent rendering is required")
    metadata["asset_origins"] = ["self_created_text", "self_created_graphic"]
    metadata["render_mode"] = "local_text_and_shapes_silent"
    return metadata
