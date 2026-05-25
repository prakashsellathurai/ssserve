from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect = urllib.request.build_opener(NoRedirectHandler)


def _get(url: str, headers: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _get_no_redirect(url: str, headers: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        resp = _no_redirect.open(req)
        return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class TestBasicServing:
    def test_serve_index(self, server_url: str):
        status, headers, body = _get(f"{server_url}/")
        assert status == 200
        assert headers.get("Content-Type", "").startswith("text/html")
        assert b"<h1>Home</h1>" in body

    def test_serve_html_file(self, server_url: str):
        status, headers, body = _get(f"{server_url}/about.html")
        assert status == 200
        assert headers.get("Content-Type", "").startswith("text/html")
        assert b"About Us" in body

    def test_serve_css(self, server_url: str):
        status, headers, body = _get(f"{server_url}/style.css")
        assert status == 200
        assert "text/css" in headers.get("Content-Type", "")
        assert b"color: red" in body

    def test_serve_json(self, server_url: str):
        status, headers, body = _get(f"{server_url}/data.json")
        assert status == 200
        assert "application/json" in headers.get("Content-Type", "").lower()
        assert b"key" in body

    def test_serve_binary_file(self, server_url: str):
        status, headers, body = _get(f"{server_url}/file-1kb.bin")
        assert status == 200
        assert len(body) == 1024

    def test_content_length_present(self, server_url: str):
        status, headers, body = _get(f"{server_url}/about.html")
        assert status == 200
        assert "Content-Length" in headers
        assert int(headers["Content-Length"]) == len(body)

    def test_404_not_found(self, server_url: str):
        status, headers, body = _get(f"{server_url}/nonexistent.html")
        assert status == 404

    def test_custom_404_page(self, server_url: str):
        status, headers, body = _get(f"{server_url}/nope")
        assert status == 404
        assert b"Custom 404" in body

    def test_head_request(self, server_url: str):
        req = urllib.request.Request(f"{server_url}/about.html", method="HEAD")
        resp = urllib.request.urlopen(req)
        assert resp.status == 200
        body = resp.read()
        assert len(body) == 0
        assert "Content-Length" in resp.headers
        assert int(resp.headers["Content-Length"]) > 0

    def test_server_header(self, server_url: str):
        _, headers, _ = _get(f"{server_url}/")
        assert "Server" in headers
        assert "ssserve" in headers["Server"]


class TestDirectoryListing:
    def test_directory_with_index(self, server_url: str):
        status, headers, body = _get(f"{server_url}/blog/")
        assert status == 200
        assert b"Blog Home" in body

    def test_directory_listing(self, server_url: str):
        status, headers, body = _get(f"{server_url}/downloads/")
        assert status == 200
        assert b"Index of /downloads" in body
        assert b"readme.txt" in body

    def test_no_directory_listing_outside_root(self, server_url: str):
        status, headers, body = _get(f"{server_url}/../etc/passwd")
        assert status in (404, 400)

    def test_unlisted_files_hidden(self, server_url: str):
        status, headers, body = _get(f"{server_url}/downloads/")
        assert status == 200
        assert b".DS_Store" not in body


class TestCleanUrls:
    def test_clean_url_redirect(self, server_url: str):
        status, headers, body = _get_no_redirect(f"{server_url}/about.html")
        assert status == 301
        assert headers.get("Location", "").endswith("/about")

    def test_clean_url_redirect_followed(self, server_url: str):
        status, headers, body = _get(f"{server_url}/about.html")
        assert status == 200
        assert b"About Us" in body

    def test_clean_url_serves_html(self, server_url: str):
        status, headers, body = _get(f"{server_url}/about")
        assert status == 200
        assert b"About Us" in body

    def test_clean_urls_disabled(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, config={"cleanUrls": False})
        status, headers, body = _get_no_redirect(f"{url}/about")
        assert status == 404


class TestTrailingSlash:
    def test_trailing_slash_redirect_when_enabled(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, config={"trailingSlash": True})
        status, headers, body = _get_no_redirect(f"{url}/blog")
        assert status == 301
        assert headers.get("Location", "").endswith("/blog/")

    def test_no_trailing_slash_when_disabled(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, config={"trailingSlash": False})
        status, headers, body = _get_no_redirect(f"{url}/blog/")
        assert status == 301
        loc = headers.get("Location", "")
        assert not loc.endswith("/")


class TestRedirectsAndRewrites:
    def test_redirect_rule_301(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, config={
            "redirects": [{"source": "/old", "destination": "/new", "type": 301}],
        })
        status, headers, body = _get_no_redirect(f"{url}/old")
        assert status == 301
        assert "new" in headers.get("Location", "")

    def test_redirect_rule_302(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, config={
            "redirects": [{"source": "/temp", "destination": "/about.html", "type": 302}],
        })
        status, headers, body = _get_no_redirect(f"{url}/temp")
        assert status == 302
        assert "about.html" in headers.get("Location", "")

    def test_rewrite_rule(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, config={
            "rewrites": [{"source": "/api/**", "destination": "/index.html"}],
        })
        status, headers, body = _get(f"{url}/api/test")
        assert status == 200
        assert b"Home" in body

    def test_rewrite_with_segments(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, config={
            "rewrites": [{"source": "/pages/:slug", "destination": "/:slug.html"}],
        })
        status, headers, body = _get(f"{url}/pages/about")
        assert status == 200
        assert b"About Us" in body


class TestCORS:
    def test_cors_headers(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--cors"])
        status, headers, body = _get(f"{url}/index.html")
        assert status == 200
        assert headers.get("Access-Control-Allow-Origin") == "*"
        assert "Access-Control-Allow-Headers" in headers
        assert "Access-Control-Allow-Methods" in headers

    def test_no_cors_by_default(self, server_url: str):
        _, headers, _ = _get(f"{server_url}/index.html")
        assert "Access-Control-Allow-Origin" not in headers

    def test_cors_options(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--cors"])
        req = urllib.request.Request(f"{url}/", method="OPTIONS")
        resp = urllib.request.urlopen(req)
        assert resp.status == 204


class TestCompression:
    def test_gzip_compression(self, server_url: str):
        status, headers, body = _get(f"{server_url}/about.html", {"Accept-Encoding": "gzip"})
        assert status == 200
        assert headers.get("Content-Encoding") == "gzip"
        assert len(body) < 100

    def test_gzip_larger_file(self, server_url: str):
        status, headers, body = _get(f"{server_url}/file-100kb.bin", {"Accept-Encoding": "gzip"})
        assert status == 200
        assert headers.get("Content-Encoding") == "gzip"

    def test_no_gzip_when_not_requested(self, server_url: str):
        status, headers, body = _get(f"{server_url}/about.html")
        assert status == 200
        assert "Content-Encoding" not in headers or headers["Content-Encoding"] != "gzip"

    def test_no_gzip_when_disabled(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--no-compression"])
        status, headers, body = _get(f"{url}/about.html", {"Accept-Encoding": "gzip"})
        assert status == 200
        assert headers.get("Content-Encoding") != "gzip"


class TestCaching:
    def test_last_modified_present(self, server_url: str):
        _, headers, _ = _get(f"{server_url}/about.html")
        assert "Last-Modified" in headers

    def test_accept_ranges_present(self, server_url: str):
        _, headers, _ = _get(f"{server_url}/about.html")
        assert headers.get("Accept-Ranges") == "bytes"

    def test_etag_enabled(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, config={"etag": True})
        _, headers, _ = _get(f"{url}/about.html")
        assert "ETag" in headers

    def test_304_not_modified_with_etag(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, config={"etag": True})
        _, headers, _ = _get(f"{url}/about.html")
        etag = headers["ETag"]
        status, headers2, body2 = _get(f"{url}/about.html", {"If-None-Match": etag})
        assert status == 304
        assert len(body2) == 0

    def test_304_not_modified_with_last_modified(self, server_url: str):
        _, headers, _ = _get(f"{server_url}/about.html")
        lm = headers["Last-Modified"]
        status, headers2, body2 = _get(f"{server_url}/about.html", {"If-Modified-Since": lm})
        assert status == 304
        assert len(body2) == 0

    def test_range_request(self, server_url: str):
        status, headers, body = _get(f"{server_url}/about.html", {"Range": "bytes=0-9"})
        assert status == 206
        assert "Content-Range" in headers
        assert len(body) == 10

    def test_range_middle_section(self, server_url: str):
        status, headers, body = _get(f"{server_url}/about.html", {"Range": "bytes=5-14"})
        assert status == 206
        assert len(body) == 10

    def test_range_suffix(self, server_url: str):
        full_status, full_headers, full_body = _get(f"{server_url}/about.html")
        total = len(full_body)
        status, headers, body = _get(f"{server_url}/about.html", {"Range": f"bytes=-{10}"})
        assert status == 206
        assert len(body) == 10
        assert body == full_body[-10:]

    def test_no_etag_flag(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--no-etag"])
        _, headers, _ = _get(f"{url}/about.html")
        assert "ETag" not in headers
        assert "Last-Modified" in headers


class TestSPAMode:
    def test_spa_mode_serves_index_for_404(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["-s"])
        status, headers, body = _get(f"{url}/some/random/path")
        assert status == 200
        assert b"Home" in body

    def test_spa_mode_404_when_no_index(self, test_dir: Path, server_factory):
        empty_dir = test_dir / "empty_spa"
        empty_dir.mkdir()
        url = server_factory(empty_dir, extra_args=["-s"])
        status, headers, body = _get(f"{url}/any/path")
        assert status == 404


class TestCustomHeaders:
    def test_custom_headers_from_config(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, config={
            "headers": [
                {"source": "**/*.html", "headers": [
                    {"key": "X-Custom", "value": "test-value"},
                    {"key": "Cache-Control", "value": "no-cache"},
                ]},
            ],
        })
        _, headers, _ = _get(f"{url}/index.html")
        assert headers.get("X-Custom") == "test-value"
        assert "no-cache" in headers.get("Cache-Control", "")


class TestSymlinks:
    def test_symlinks_disabled_by_default(self, test_dir: Path, server_factory):
        target = test_dir / "index.html"
        link = test_dir / "linked.html"
        link.symlink_to(target)
        url = server_factory(test_dir)
        status, headers, body = _get(f"{url}/linked.html")
        assert status == 404

    def test_symlinks_enabled(self, test_dir: Path, server_factory):
        target = test_dir / "index.html"
        link = test_dir / "linked.html"
        link.symlink_to(target)
        url = server_factory(test_dir, extra_args=["-S"])
        status, headers, body = _get(f"{url}/linked.html")
        assert status == 200
        assert b"Home" in body


class TestSecurity:
    def test_path_traversal_blocked(self, server_url: str):
        status, headers, body = _get(f"{server_url}/../../../etc/passwd")
        assert status in (404, 400)

    def test_dot_git_not_listed(self, test_dir: Path, server_factory):
        (test_dir / ".git").mkdir()
        url = server_factory(test_dir)
        status, headers, body = _get(f"{url}/")
        assert b".git" not in body
