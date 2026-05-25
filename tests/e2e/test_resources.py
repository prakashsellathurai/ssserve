from __future__ import annotations

import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import psutil
import pytest


def _find_server_pid(port: int) -> int | None:
    for conn in psutil.net_connections():
        if hasattr(conn.laddr, "port") and conn.laddr.port == port and conn.status == "LISTEN":
            return conn.pid
    return None


def _serve_requests(url: str, n: int, concurrency: int):
    def _req(_):
        resp = urllib.request.urlopen(url)
        resp.read()

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = [ex.submit(_req, i) for i in range(n)]
        for _ in as_completed(futures):
            pass


_MAX_RSS_MB = 100
_MAX_FDS = 200


class TestMemory:
    def test_memory_idle(self, server_url: str):
        port = int(server_url.split(":")[-1])
        pid = _find_server_pid(port)
        assert pid is not None, f"Could not find server PID for port {port}"
        proc = psutil.Process(pid)

        time.sleep(0.5)
        rss = proc.memory_info().rss
        rss_mb = rss / 1024 / 1024
        print(f"\n  RSS (idle): {rss_mb:.1f} MB")
        assert rss_mb < _MAX_RSS_MB, f"RSS {rss_mb:.1f} MB ≥ {_MAX_RSS_MB} MB"

    def test_memory_under_single_request(self, server_url: str):
        port = int(server_url.split(":")[-1])
        pid = _find_server_pid(port)
        assert pid is not None
        proc = psutil.Process(pid)

        rss_before = proc.memory_info().rss
        for _ in range(5):
            urllib.request.urlopen(f"{server_url}/file-5mb.bin").read()
        rss_after = proc.memory_info().rss

        delta_mb = (rss_after - rss_before) / 1024 / 1024
        print(
            f"\n  Memory — single request (5MB file, 5 iterations):"
            f"\n    Before: {rss_before/1024/1024:.1f} MB"
            f"\n    After:  {rss_after/1024/1024:.1f} MB"
            f"\n    Delta:  {delta_mb:+.1f} MB"
        )
        assert rss_after / 1024 / 1024 < _MAX_RSS_MB, (
            f"RSS after load {rss_after/1024/1024:.1f} MB ≥ {_MAX_RSS_MB} MB"
        )

    def test_memory_under_concurrent_load(self, server_url: str):
        port = int(server_url.split(":")[-1])
        pid = _find_server_pid(port)
        assert pid is not None
        proc = psutil.Process(pid)

        rss_before = proc.memory_info().rss
        _serve_requests(f"{server_url}/file-100kb.bin", n=50, concurrency=25)
        time.sleep(0.5)
        rss_after = proc.memory_info().rss

        delta_mb = (rss_after - rss_before) / 1024 / 1024
        print(
            f"\n  Memory — concurrent load (100KB, 50x @ 25 concurrency):"
            f"\n    Before: {rss_before/1024/1024:.1f} MB"
            f"\n    After:  {rss_after/1024/1024:.1f} MB"
            f"\n    Delta:  {delta_mb:+.1f} MB"
        )
        assert rss_after / 1024 / 1024 < _MAX_RSS_MB, (
            f"RSS after load {rss_after/1024/1024:.1f} MB ≥ {_MAX_RSS_MB} MB"
        )
        assert delta_mb < 50, f"RSS grew by {delta_mb:+.1f} MB (limit 50 MB)"


class TestCPU:
    def test_cpu_idle(self, server_url: str):
        port = int(server_url.split(":")[-1])
        pid = _find_server_pid(port)
        assert pid is not None
        proc = psutil.Process(pid)

        cpu = proc.cpu_percent(interval=2.0)
        print(f"\n  CPU (idle, 2s interval): {cpu:.1f}%")
        assert cpu < 10, f"CPU idle {cpu:.1f}% ≥ 10%"

    def test_cpu_under_load(self, server_url: str):
        port = int(server_url.split(":")[-1])
        pid = _find_server_pid(port)
        assert pid is not None
        proc = psutil.Process(pid)

        proc.cpu_percent(interval=0.1)
        with ThreadPoolExecutor(max_workers=10) as ex:
            def _load(i):
                for _ in range(20):
                    urllib.request.urlopen(f"{server_url}/file-1kb.bin").read()

            futures = [ex.submit(_load, i) for i in range(10)]
            time.sleep(1.0)
            cpu = proc.cpu_percent(interval=2.0)
            for f in futures:
                f.result()

        print(f"\n  CPU (under load, 10 concurrent x 20 reqs, 2s interval): {cpu:.1f}%")
        assert cpu < 100, f"CPU under load {cpu:.1f}% ≥ 100% (single process limit)"


class TestFileDescriptors:
    def test_fd_count_idle(self, server_url: str):
        port = int(server_url.split(":")[-1])
        pid = _find_server_pid(port)
        assert pid is not None
        proc = psutil.Process(pid)

        fds = proc.num_fds()
        print(f"\n  File descriptors (idle): {fds}")
        assert fds < _MAX_FDS, f"FDs {fds} ≥ {_MAX_FDS}"

    def test_fd_count_under_load(self, server_url: str):
        port = int(server_url.split(":")[-1])
        pid = _find_server_pid(port)
        assert pid is not None
        proc = psutil.Process(pid)

        fds_before = proc.num_fds()
        _serve_requests(f"{server_url}/file-1kb.bin", n=100, concurrency=25)
        time.sleep(0.5)
        fds_after = proc.num_fds()

        print(
            f"\n  File descriptors — under load (100 reqs @ 25 concurrency):"
            f"\n    Before: {fds_before}"
            f"\n    After:  {fds_after}"
            f"\n    Delta:  {fds_after - fds_before:+d}"
        )
        assert fds_after < _MAX_FDS, f"FDs under load {fds_after} ≥ {_MAX_FDS}"
