from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _populate_test_dir(base: Path) -> None:
    (base / "index.html").write_text("<h1>Home</h1>")
    (base / "about.html").write_text("<h1>About Us</h1>")
    (base / "style.css").write_text("body { color: red; }")
    (base / "script.js").write_text("console.log('test');")
    (base / "data.json").write_text('{"key": "value"}')
    (base / "404.html").write_text("<h1>Custom 404</h1>")
    (base / "file-1kb.bin").write_bytes(b"x" * 1024)
    (base / "file-100kb.bin").write_bytes(b"x" * (100 * 1024))
    (base / "file-5mb.bin").write_bytes(b"x" * (5 * 1024 * 1024))

    blog = base / "blog"
    blog.mkdir()
    (blog / "index.html").write_text("<h1>Blog Home</h1>")
    (blog / "post.html").write_text("<h1>Blog Post</h1>")

    dl = base / "downloads"
    dl.mkdir()
    (dl / "readme.txt").write_text("hello world")


def _wait_for_server(port: int, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"Server exited prematurely with code {proc.returncode}")
        try:
            with socket.create_connection(("localhost", port), timeout=0.5) as s:
                s.sendall(b"GET / HTTP/1.0\r\n\r\n")
                if b"HTTP/" in s.recv(128):
                    return
        except (ConnectionRefusedError, OSError, socket.timeout):
            time.sleep(0.1)
    pytest.fail(f"Server did not start on port {port} within {timeout}s")


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _start_server(
    serve_dir: Path,
    extra_args: list[str] | None = None,
    config: dict | None = None,
) -> tuple[subprocess.Popen, int]:
    if config:
        (serve_dir / "serve.json").write_text(json.dumps(config))
    port = find_free_port()
    venv_python = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    args = (
        [python, "-m", "ssserve", str(serve_dir), "-l", str(port), "--no-port-switching", "-L"]
        + (extra_args or [])
    )
    proc = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_server(port, proc)
    return proc, port


@pytest.fixture
def test_dir(tmp_path: Path) -> Path:
    _populate_test_dir(tmp_path)
    return tmp_path


@pytest.fixture
def server_url(test_dir: Path) -> str:
    proc, port = _start_server(test_dir)
    yield f"http://localhost:{port}"
    _stop_server(proc)


@pytest.fixture
def server_factory() -> type:
    started = []

    def _start(serve_dir: Path, extra_args: list[str] | None = None, config: dict | None = None) -> str:
        proc, port = _start_server(serve_dir, extra_args=extra_args, config=config)
        started.append(proc)
        return f"http://localhost:{port}"

    yield _start

    for p in started:
        try:
            _stop_server(p)
        except Exception:
            pass
