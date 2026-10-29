"""Start and probe the empty API, Web and worker entry points."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import os
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def probe(command: list[str], cwd: Path, url: str, expected: bytes, *, env: dict[str, str]) -> None:
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Server exited early: {command}")
            try:
                with urlopen(url, timeout=1) as response:
                    assert response.status == 200
                    assert expected in response.read()
                    print(f"OK {url}")
                    return
            except OSError:
                time.sleep(0.2)
        raise TimeoutError(f"Server did not respond: {url}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> None:
    # Smoke checks must never claim a task from the user's local workspace.
    with tempfile.TemporaryDirectory(prefix="book-video-smoke-") as temporary:
        env = os.environ.copy()
        env["BOOK_VIDEO_DATABASE_PATH"] = str(Path(temporary) / "smoke.sqlite3")
        env["BOOK_VIDEO_OUTPUT_DIR"] = str(Path(temporary) / "renders")
        api_port = free_port()
        probe(
            [sys.executable, "-m", "uvicorn", "book_video.main:app", "--host", "127.0.0.1", "--port", str(api_port)],
            ROOT,
            f"http://127.0.0.1:{api_port}/healthz",
            b'"status":"ok"',
            env=env,
        )
        worker = subprocess.run(
            [sys.executable, "-m", "book_video.worker", "--once"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert "worker ready" in worker.stdout
        print("OK worker")
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("npm is required to verify the Web entry point")
        web_port = free_port()
        probe(
            [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(web_port), "--strictPort"],
            ROOT / "apps" / "web",
            f"http://127.0.0.1:{web_port}/",
            "图书短视频工作台".encode(),
            env=env,
        )


if __name__ == "__main__":
    main()
