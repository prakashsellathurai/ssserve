from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


def _get(url: str, headers: dict | None = None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        resp = urllib.request.urlopen(req)
        return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class TestLiveReload:
    def test_injects_script_with_flag(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--live-reload"])
        status, headers, body = _get(f"{url}/")
        assert status == 200
        assert b"__ssserve/lr-check" in body
        assert b"location.reload" in body

    def test_no_injection_without_flag(self, server_url: str):
        _, _, body = _get(f"{server_url}/")
        assert b"__ssserve/lr-check" not in body

    def test_check_endpoint_returns_json(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--live-reload"])
        status, headers, body = _get(f"{url}/__ssserve/lr-check")
        assert status == 200
        assert headers.get("Content-Type", "").startswith("application/json")
        data = json.loads(body)
        assert "reload" in data
        assert isinstance(data["reload"], bool)
        assert "version" in data
        assert isinstance(data["version"], int)

    def test_no_injection_in_non_html(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--live-reload"])
        _, _, body = _get(f"{url}/style.css")
        assert b"__ssserve/lr-check" not in body

    def test_injection_in_directory_listing(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--live-reload"])
        _, _, body = _get(f"{url}/downloads/")
        assert b"__ssserve/lr-check" in body

    def test_check_initial_no_reload(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--live-reload"])
        _, _, body = _get(f"{url}/__ssserve/lr-check")
        data = json.loads(body)
        assert data["reload"] is False

    def test_file_change_triggers_reload(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--live-reload"])
        _, _, body = _get(f"{url}/__ssserve/lr-check")
        v1 = json.loads(body)["version"]
        (test_dir / "index.html").write_text("<h1>Modified</h1>")
        time.sleep(2)
        _, _, body = _get(f"{url}/__ssserve/lr-check")
        data = json.loads(body)
        assert data["version"] > v1
        assert data["reload"] is True

    def test_check_with_correct_version_no_reload(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--live-reload"])
        _, _, body = _get(f"{url}/__ssserve/lr-check")
        v = json.loads(body)["version"]
        _, _, body = _get(f"{url}/__ssserve/lr-check?v=" + str(v))
        data = json.loads(body)
        assert data["reload"] is False

    def test_injected_script_updates_version_after_poll(self, test_dir: Path, server_factory):
        url = server_factory(test_dir, extra_args=["--live-reload"])
        status, headers, body = _get(f"{url}/")
        assert status == 200
        html = body.decode("utf-8")
        assert "lr-check?v=" in html
