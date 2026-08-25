from __future__ import annotations

import json
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
            with socket.create_connection(("127.0.0.1", port), timeout=0.5) as s:
                s.sendall(b"GET / HTTP/1.0\r\n\r\n")
                if b"HTTP/" in s.recv(128):
                    return
        except (ConnectionRefusedError, OSError, socket.timeout):
            time.sleep(0.02)
    pytest.fail(f"Server did not start on port {port} within {timeout}s")


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _start_c_thread_server(
    serve_dir: Path,
    extra_files: dict[str, str | bytes] | None = None,
    **kwargs,
) -> tuple[str, subprocess.Popen | None]:
    try:
        from ssserve._server import serve
    except ImportError:
        pytest.skip("C server extension not built")

    if extra_files:
        for name, content in extra_files.items():
            p = serve_dir / name
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content)

    port = find_free_port()
    server_thread = threading.Thread(
        target=serve,
        args=(port, str(serve_dir)),
        kwargs=kwargs,
        daemon=True,
    )
    server_thread.start()

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5) as s:
                s.sendall(b"GET / HTTP/1.0\r\n\r\n")
                if b"HTTP/" in s.recv(128):
                    break
        except (ConnectionRefusedError, OSError, socket.timeout):
            time.sleep(0.02)
    else:
        pytest.fail(f"C server did not start on port {port} within 5s")

    return f"http://127.0.0.1:{port}", None


@pytest.fixture
def test_dir(tmp_path: Path) -> Path:
    (tmp_path / "index.html").write_text("<h1>Home</h1>")
    (tmp_path / "file-1kb.bin").write_bytes(b"x" * 1024)
    (tmp_path / "404.html").write_text("<h1>Custom 404</h1>")
    (tmp_path / "clean.html").write_text("<h1>Clean Page</h1>")
    parent = tmp_path.parent / "escape_test.txt"
    parent.write_text("escaped")
    return tmp_path


@pytest.fixture
def c_server(test_dir: Path) -> str:
    url, _ = _start_c_thread_server(test_dir, cors=False, caching=False, etag=False)
    return url


@pytest.fixture
def c_server_etag(test_dir: Path) -> str:
    url, _ = _start_c_thread_server(test_dir, cors=False, caching=False, etag=True)
    return url


@pytest.fixture
def c_server_no_index(tmp_path: Path) -> str:
    url, _ = _start_c_thread_server(
        tmp_path,
        extra_files={"file-1kb.bin": b"x" * 1024, "another.txt": "hello"},
        cors=False, caching=False, etag=False,
    )
    return url, tmp_path


@pytest.fixture
def c_server_no_404(tmp_path: Path) -> str:
    url, _ = _start_c_thread_server(
        tmp_path,
        extra_files={"index.html": "<h1>Home</h1>"},
        cors=False, caching=False, etag=False,
    )
    return url


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
        status, _, _ = _get(f"{c_server}/%2e%2e/%2e%2e/%2e%2e/etc/passwd")
        assert status in (400, 403), f"Encoded path traversal not rejected, got {status}"


class TestCServerETag:
    def test_c_server_etag_header(self, c_server_etag: str):
        status, headers, body = _get(f"{c_server_etag}/file-1kb.bin")
        assert status == 200
        assert "ETag" in headers, "ETag header missing from response"
        etag = headers["ETag"]
        assert etag.startswith('"') and etag.endswith('"'), f"ETag not quoted: {etag}"
        inner = etag.strip('"')
        parts = inner.split("-")
        assert len(parts) >= 2, f"ETag format invalid: {etag}"

    def test_c_server_etag_304(self, c_server_etag: str):
        status, headers, body = _get(f"{c_server_etag}/file-1kb.bin")
        assert status == 200
        etag = headers["ETag"]
        assert etag, "No ETag in response"

        status2, headers2, body2 = _get(
            f"{c_server_etag}/file-1kb.bin",
            headers={"If-None-Match": etag},
        )
        assert status2 == 304, f"Expected 304 Not Modified, got {status2}"
        assert len(body2) == 0, f"304 response should have empty body, got {len(body2)} bytes"

    def test_c_server_gzip(self, c_server_etag: str):
        req = urllib.request.Request(
            f"{c_server_etag}/index.html",
            headers={"Accept-Encoding": "gzip"},
        )
        resp = urllib.request.urlopen(req)
        body = resp.read()
        ce = resp.headers.get("Content-Encoding", "")
        assert ce == "gzip", f"Expected Content-Encoding: gzip, got '{ce}'"
        assert len(body) > 0, "Empty gzipped body"
        assert body[:2] == b"\x1f\x8b", "Response not valid gzip"

    def test_c_server_last_modified(self, c_server: str):
        status, headers, body = _get(f"{c_server}/file-1kb.bin")
        assert status == 200
        assert "Last-Modified" in headers, "Last-Modified header missing"


def test_c_server_directory_listing(c_server_no_index):
    url, test_dir = c_server_no_index
    resp = urllib.request.urlopen(f'{url}/')
    assert resp.status == 200
    html = resp.read().decode()
    assert 'Index of /' in html
    assert 'file-1kb.bin' in html


def test_c_server_directory_listing_sorted(c_server_no_index):
    url, test_dir = c_server_no_index
    resp = urllib.request.urlopen(f'{url}/')
    html = resp.read().decode()
    assert '<tr' in html


def test_c_server_custom_404_page(c_server: str):
    status, headers, body = _get(f"{c_server}/nonexistent.txt")
    assert status == 404
    assert b"Custom 404" in body


def test_c_server_error_page_content_type(c_server: str):
    status, headers, body = _get(f"{c_server}/nonexistent.txt")
    assert status == 404
    assert headers.get("Content-Type") == "text/html; charset=utf-8"


def test_c_server_error_page_html_body(c_server_no_404: str):
    status, headers, body = _get(f"{c_server_no_404}/nonexistent.txt")
    assert status == 404
    html = body.decode()
    assert "<!doctype html>" in html.lower()
    assert "404" in html


def test_c_server_builtin_404_page(c_server_no_404: str):
    status, headers, body = _get(f"{c_server_no_404}/nonexistent.txt")
    assert status == 404
    assert headers.get("Content-Type") == "text/html; charset=utf-8"
    html = body.decode()
    assert "404" in html
    assert "Not Found" in html


def test_c_server_directory_trailing_slash_redirect(c_server_no_index):
    url, test_dir = c_server_no_index
    import os
    os.makedirs(os.path.join(test_dir, 'subdir'), exist_ok=True)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def http_error_301(self, req, fp, code, msg, headers):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        opener.open(f'{url}/subdir')
    assert exc_info.value.code == 301


def _config_callback(path: str, *, redirects=None, rewrites=None):
    import re as re_mod

    if redirects is None:
        redirects = []
    if rewrites is None:
        rewrites = []

    for rule in redirects:
        m = rule["compiled_regex"].match(path)
        if m:
            dest = rule["destination"]
            for key, val in m.groupdict().items():
                dest = dest.replace(f":{key}", val)
            return dest, rule["type"]

    for rule in rewrites:
        m = rule["compiled_regex"].match(path)
        if m:
            dest = rule["destination"]
            for key, val in m.groupdict().items():
                dest = dest.replace(f":{key}", val)
            return dest, 200

    return None


@pytest.fixture
def c_server_with_config(tmp_path: Path, request):
    try:
        from ssserve._server import serve
    except ImportError:
        pytest.skip("C server extension not built")

    (tmp_path / "index.html").write_text("<h1>Home</h1>")
    (tmp_path / "new.html").write_text("<h1>New Page</h1>")
    (tmp_path / "app.html").write_text("<h1>App Page</h1>")

    marker = request.node.get_closest_marker("c_server_config")
    config = marker.kwargs if marker else {}

    redirects_raw = config.get("redirects", [])
    rewrites_raw = config.get("rewrites", [])

    import re as re_mod

    def _route_to_regex(pattern):
        parts = []
        for segment in pattern.split("/"):
            if segment.startswith(":"):
                parts.append(f"(?P<{segment[1:]}>[^/]+)")
            elif "*" in segment:
                parts.append(re_mod.escape(segment).replace(r"\*\*", ".*").replace(r"\*", "[^/]*"))
            else:
                parts.append(re_mod.escape(segment))
        return re_mod.compile(f"^{'/'.join(parts)}$")

    redirects = []
    for r in redirects_raw:
        redirects.append({
            "source": r["source"],
            "destination": r["destination"],
            "type": r.get("type", 301),
            "compiled_regex": _route_to_regex(r["source"]),
        })

    rewrites = []
    for r in rewrites_raw:
        rewrites.append({
            "source": r["source"],
            "destination": r["destination"],
            "compiled_regex": _route_to_regex(r["source"]),
        })

    def callback(path):
        return _config_callback(path, redirects=redirects, rewrites=rewrites)

    port = find_free_port()
    server_thread = threading.Thread(
        target=serve,
        args=(port, str(tmp_path)),
        kwargs={"cors": False, "caching": False, "etag": False, "config_callback": callback},
        daemon=True,
    )
    server_thread.start()

    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5) as s:
                s.sendall(b"GET / HTTP/1.0\r\n\r\n")
                if b"HTTP/" in s.recv(128):
                    break
        except (ConnectionRefusedError, OSError, socket.timeout):
            time.sleep(0.02)
    else:
        pytest.fail(f"C server (config) did not start on port {port} within 5s")

    yield f"http://127.0.0.1:{port}"


@pytest.mark.c_server_config(
    redirects=[{"source": "/old", "destination": "/new", "type": 301}]
)
def test_c_server_redirect(c_server_with_config):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def http_error_301(self, req, fp, code, msg, headers):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        opener.open(f'{c_server_with_config}/old')
    assert exc_info.value.code == 301
    assert exc_info.value.headers.get('Location') == '/new'


@pytest.mark.c_server_config(
    rewrites=[{"source": "/app", "destination": "/app.html"}]
)
def test_c_server_rewrite(c_server_with_config):
    resp = urllib.request.urlopen(f'{c_server_with_config}/app')
    assert resp.status == 200
    body = resp.read().decode()
    assert "App Page" in body


@pytest.fixture
def c_server_range(tmp_path: Path) -> str:
    url, _ = _start_c_thread_server(
        tmp_path,
        extra_files={"file-100kb.bin": b"x" * 102400},
        cors=False, caching=False, etag=False,
    )
    return url


def test_c_server_range_request(c_server_range):
    req = urllib.request.Request(f'{c_server_range}/file-100kb.bin',
                                headers={'Range': 'bytes=0-1023'})
    resp = urllib.request.urlopen(req)
    assert resp.status == 206
    assert resp.headers.get('Content-Range') == 'bytes 0-1023/102400'
    data = resp.read()
    assert len(data) == 1024


def test_c_server_range_end_only(c_server_range):
    req = urllib.request.Request(f'{c_server_range}/file-100kb.bin',
                                headers={'Range': 'bytes=-1024'})
    resp = urllib.request.urlopen(req)
    assert resp.status == 206
    data = resp.read()
    assert len(data) == 1024


def test_c_server_range_invalid(c_server_range):
    req = urllib.request.Request(f'{c_server_range}/file-100kb.bin',
                                headers={'Range': 'bytes=999999-999999'})
    resp = urllib.request.urlopen(req)
    assert resp.status == 200


def test_c_server_cors_headers(test_dir: Path):
    url, _ = _start_c_thread_server(test_dir, cors=True, caching=False, etag=False)
    req = urllib.request.Request(f'{url}/file-1kb.bin',
                                headers={'Origin': 'http://example.com'})
    resp = urllib.request.urlopen(req)
    assert resp.headers.get('Access-Control-Allow-Origin') == '*'


def test_c_server_no_cache_header(test_dir: Path):
    url, _ = _start_c_thread_server(test_dir, cors=False, caching=False, etag=False)
    resp = urllib.request.urlopen(f'{url}/file-1kb.bin')
    assert resp.headers.get('Cache-Control') == 'no-store'


def test_c_server_clean_url_redirect(c_server: str):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def http_error_301(self, req, fp, code, msg, headers):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        opener.open(f'{c_server}/clean.html')
    assert exc_info.value.code == 301


def test_c_server_no_compression(tmp_path: Path):
    url, _ = _start_c_thread_server(
        tmp_path,
        extra_files={"index.html": "<h1>Home</h1>" * 100},
        cors=False, caching=False, etag=False, no_compression=True,
    )
    req = urllib.request.Request(
        f'{url}/index.html',
        headers={"Accept-Encoding": "gzip"},
    )
    resp = urllib.request.urlopen(req)
    body = resp.read()
    ce = resp.headers.get("Content-Encoding", "")
    assert ce != "gzip", f"Expected no gzip, got Content-Encoding: '{ce}'"


def test_c_server_caching_enabled(tmp_path: Path):
    url, _ = _start_c_thread_server(
        tmp_path,
        extra_files={"index.html": "<h1>Home</h1>"},
        cors=False, caching=True, etag=False,
    )
    resp = urllib.request.urlopen(f'{url}/index.html')
    assert resp.headers.get('Cache-Control') != 'no-store'
