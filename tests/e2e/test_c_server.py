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
    # Create a file that could be accessed via traversal if not blocked
    parent = tmp_path.parent / "escape_test.txt"
    parent.write_text("escaped")
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

    def test_c_server_path_traversal_rejected(self, c_server: str):
        status, _, _ = _get(f"{c_server}/../../../etc/passwd")
        # Should be rejected (400/403), not just not-found (404)
        assert status in (400, 403), f"Path traversal not rejected, got {status}"

    def test_c_server_double_slash_normalized(self, c_server: str):
        status, _, body = _get(f"{c_server}//file-1kb.bin")
        assert status == 200
        assert len(body) == 1024

    def test_c_server_percent_encoded(self, c_server: str):
        status, _, body = _get(f"{c_server}/file%2D1kb.bin")
        assert status == 200
        assert len(body) == 1024

    def test_c_server_dot_segments(self, c_server: str):
        status, _, body = _get(f"{c_server}/./file-1kb.bin")
        assert status == 200
        assert len(body) == 1024

    def test_c_server_null_byte_rejected(self, c_server: str):
        status, _, _ = _get(f"{c_server}/file%00.html")
        assert status in (400, 403, 404), f"Null byte not rejected, got {status}"

    def test_c_server_encoded_traversal_rejected(self, c_server: str):
        # %2e%2e = .. (encoded dots)
        status, _, _ = _get(f"{c_server}/%2e%2e/%2e%2e/%2e%2e/etc/passwd")
        assert status in (400, 403), f"Encoded path traversal not rejected, got {status}"


@pytest.fixture
def c_server_etag(test_dir: Path) -> str:
    try:
        from ssserve._server import serve
    except ImportError:
        pytest.skip("C server extension not built")

    port = find_free_port()
    server_thread = threading.Thread(
        target=serve,
        args=(port, str(test_dir)),
        kwargs={"cors": False, "caching": False, "etag": True},
        daemon=True,
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
        pytest.fail(f"C server (etag) did not start on port {port} within 5s")

    yield f"http://localhost:{port}"


class TestCServerETag:
    def test_c_server_etag_header(self, c_server_etag: str):
        status, headers, body = _get(f"{c_server_etag}/file-1kb.bin")
        assert status == 200
        assert "ETag" in headers, "ETag header missing from response"
        etag = headers["ETag"]
        assert etag.startswith('"') and etag.endswith('"'), f"ETag not quoted: {etag}"
        # ETag format: "mtime-size"
        inner = etag.strip('"')
        parts = inner.split("-")
        assert len(parts) >= 2, f"ETag format invalid: {etag}"

    def test_c_server_etag_304(self, c_server_etag: str):
        # First request to get the ETag
        status, headers, body = _get(f"{c_server_etag}/file-1kb.bin")
        assert status == 200
        etag = headers["ETag"]
        assert etag, "No ETag in response"

        # Second request with matching If-None-Match -> should be 304
        status2, headers2, body2 = _get(
            f"{c_server_etag}/file-1kb.bin",
            headers={"If-None-Match": etag},
        )
        assert status2 == 304, f"Expected 304 Not Modified, got {status2}"
        assert len(body2) == 0, f"304 response should have empty body, got {len(body2)} bytes"

    def test_c_server_gzip(self, c_server_etag: str):
        # Create a larger file for meaningful compression
        import urllib.request

        req = urllib.request.Request(
            f"{c_server_etag}/index.html",
            headers={"Accept-Encoding": "gzip"},
        )
        resp = urllib.request.urlopen(req)
        body = resp.read()
        ce = resp.headers.get("Content-Encoding", "")
        assert ce == "gzip", f"Expected Content-Encoding: gzip, got '{ce}'"
        assert len(body) > 0, "Empty gzipped body"

        # Verify actual gzip magic bytes
        assert body[:2] == b"\x1f\x8b", "Response not valid gzip"

    def test_c_server_last_modified(self, c_server: str):
        status, headers, body = _get(f"{c_server}/file-1kb.bin")
        assert status == 200
        # When ETag is disabled, Last-Modified should be present
        assert "Last-Modified" in headers, "Last-Modified header missing"
