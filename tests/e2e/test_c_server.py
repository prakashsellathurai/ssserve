from __future__ import annotations

import threading
import urllib.request
import urllib.error
import pytest
import subprocess
import socket
import time
import sys
from pathlib import Path


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


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


@pytest.fixture
def test_dir(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<h1>Home</h1>")
    (tmp_path / "file-1kb.bin").write_bytes(b"x" * 1024)
    (tmp_path / "404.html").write_text("<h1>Custom 404</h1>")
    return tmp_path


@pytest.fixture
def c_server(test_dir: Path) -> str:
    try:
        from ssserve._server import serve
    except ImportError:
        pytest.skip("C server extension not built")
    
    port = find_free_port()
    server_thread = threading.Thread(
        target=serve,
        args=(port, str(test_dir)),
        kwargs={"cors": False, "caching": False, "etag": False},
        daemon=True
    )
    server_thread.start()
    
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("localhost", port), timeout=0.5) as s:
                s.sendall(b"GET / HTTP/1.0\r\n\r\n")
                if b"HTTP/" in s.recv(128):
                    break
        except (ConnectionRefusedError, OSError, socket.timeout):
            time.sleep(0.1)
    else:
        pytest.fail(f"C server did not start on port {port} within 5s")
    
    yield f"http://localhost:{port}"


def _get(url: str, headers: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class TestCServer:
    def test_c_server_serves_file(self, c_server: str):
        status, headers, body = _get(f"{c_server}/file-1kb.bin")
        assert status == 200
        assert len(body) == 1024
        assert "Content-Length" in headers
        assert int(headers["Content-Length"]) == 1024

    def test_c_server_returns_404(self, c_server: str):
        status, headers, body = _get(f"{c_server}/nonexistent.html")
        assert status == 404

    def test_c_server_head_request(self, c_server: str):
        req = urllib.request.Request(f"{c_server}/file-1kb.bin", method="HEAD")
        resp = urllib.request.urlopen(req)
        assert resp.status == 200
        body = resp.read()
        assert len(body) == 0
        assert "Content-Length" in resp.headers
        assert int(resp.headers["Content-Length"]) == 1024
