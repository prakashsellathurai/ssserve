from __future__ import annotations

import cProfile
import json
import os
import pstats
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"


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

    many = base / "many-files"
    many.mkdir()
    for i in range(100):
        (many / f"file-{i:03d}.txt").write_text(f"file {i}")


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


def _find_server_pid(port: int) -> int | None:
    import psutil

    for conn in psutil.net_connections():
        if hasattr(conn.laddr, "port") and conn.laddr.port == port and conn.status == "LISTEN":
            return conn.pid
    return None


def _warm_up(url: str, requests: int = 5) -> None:
    for _ in range(requests):
        try:
            resp = urllib.request.urlopen(f"{url}/")
            resp.read()
        except Exception:
            pass


@pytest.fixture
def profiled_server(test_dir: Path, request) -> tuple[str, Path]:
    proc, port = _start_server(test_dir)
    url = f"http://localhost:{port}"

    _warm_up(url)

    profiler = cProfile.Profile()
    profiler.enable()

    yield url, PROFILES_DIR

    profiler.disable()
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROFILES_DIR / f"{request.node.name}.prof"
    profiler.dump_stats(str(out_path))
    stats = pstats.Stats(str(out_path))
    print(f"\n  CPU profile saved: {out_path}")
    stats.print_stats(20)
    _stop_server(proc)


@pytest.fixture
def memray_server(test_dir: Path, request) -> tuple[str, Path]:
    proc, port = _start_server(test_dir)
    url = f"http://localhost:{port}"

    try:
        import memray
    except ImportError:
        pytest.skip("memray not installed")
        yield url, PROFILES_DIR
        _stop_server(proc)
        return

    _warm_up(url)

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PROFILES_DIR / f"{request.node.name}.bin"
    out_path.unlink(missing_ok=True)
    tracker = memray.Tracker(str(out_path))

    with tracker:
        yield url, PROFILES_DIR

    print(f"\n  Memory profile saved: {out_path}")
    _stop_server(proc)


@pytest.fixture
def sampled_server(test_dir: Path, request) -> tuple[str, Path]:
    proc, port = _start_server(test_dir)
    url = f"http://localhost:{port}"

    if not shutil.which("py-spy"):
        pytest.skip("py-spy not installed")
        yield url, PROFILES_DIR
        _stop_server(proc)
        return

    _warm_up(url)

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    svg_path = PROFILES_DIR / f"{request.node.name}.svg"
    pid = _find_server_pid(port)

    duration = getattr(request, "param", {}).get("duration", 5)
    rate = getattr(request, "param", {}).get("rate", 100)

    if pid:
        record_proc = subprocess.Popen(
            ["py-spy", "record", "-o", str(svg_path), "-p", str(pid),
             "--duration", str(duration), "--rate", str(rate)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        record_proc = None

    yield url, PROFILES_DIR

    if record_proc:
        try:
            record_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            record_proc.terminate()
            record_proc.wait(timeout=5)
        if svg_path.exists():
            print(f"\n  Sampling profile saved: {svg_path}")
        else:
            print(f"\n  Sampling profile: no stacks collected (process too short-lived)")
    _stop_server(proc)
