from __future__ import annotations

import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest


def _measure_latency(url: str, n: int = 20) -> dict:
    latencies = []
    for _ in range(n):
        start = time.perf_counter()
        resp = urllib.request.urlopen(url)
        resp.read()
        latencies.append(time.perf_counter() - start)
    latencies.sort()
    return {
        "n": n,
        "p50": latencies[len(latencies) // 2],
        "p95": latencies[int(len(latencies) * 0.95)],
        "p99": latencies[int(len(latencies) * 0.99)],
        "mean": sum(latencies) / n,
        "min": min(latencies),
        "max": max(latencies),
    }


def _measure_throughput(url: str, n_requests: int = 50, n_concurrent: int = 10) -> dict:
    latencies = []
    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n_concurrent) as ex:
        def _req(_):
            start = time.perf_counter()
            resp = urllib.request.urlopen(url)
            resp.read()
            return time.perf_counter() - start

        futures = [ex.submit(_req, i) for i in range(n_requests)]
        for f in as_completed(futures):
            latencies.append(f.result())
    wall_time = time.perf_counter() - wall_start
    latencies.sort()
    return {
        "n_requests": n_requests,
        "n_concurrent": n_concurrent,
        "wall_time_s": round(wall_time, 3),
        "throughput_req_per_sec": round(n_requests / wall_time, 1) if wall_time > 0 else 0,
        "p50": latencies[len(latencies) // 2],
        "p95": latencies[int(len(latencies) * 0.95)] if len(latencies) > 1 else latencies[0],
    }


_P50_THRESHOLD_MS = 20
_P95_THRESHOLD_MS = 50
_P99_THRESHOLD_MS = 200


class TestFileLatency:
    def test_small_file_latency(self, server_url: str):
        url = f"{server_url}/file-1kb.bin"
        r = _measure_latency(url, n=30)
        p50_ms = r["p50"] * 1000
        p95_ms = r["p95"] * 1000
        print(
            f"\n  Small file (1KB) latency [n={r['n']}]:"
            f"\n    p50={p50_ms:.1f}ms  p95={p95_ms:.1f}ms"
            f"\n    p99={r['p99']*1000:.1f}ms  mean={r['mean']*1000:.1f}ms"
            f"\n    min={r['min']*1000:.1f}ms  max={r['max']*1000:.1f}ms"
        )
        assert p50_ms < _P50_THRESHOLD_MS, f"p50 {p50_ms:.1f}ms ≥ {_P50_THRESHOLD_MS}ms"
        assert p95_ms < _P95_THRESHOLD_MS, f"p95 {p95_ms:.1f}ms ≥ {_P95_THRESHOLD_MS}ms"

    def test_medium_file_latency(self, server_url: str):
        url = f"{server_url}/file-100kb.bin"
        r = _measure_latency(url, n=20)
        p50_ms = r["p50"] * 1000
        p95_ms = r["p95"] * 1000
        print(
            f"\n  Medium file (100KB) latency [n={r['n']}]:"
            f"\n    p50={p50_ms:.1f}ms  p95={p95_ms:.1f}ms"
            f"\n    p99={r['p99']*1000:.1f}ms  mean={r['mean']*1000:.1f}ms"
            f"\n    min={r['min']*1000:.1f}ms  max={r['max']*1000:.1f}ms"
        )
        assert p50_ms < _P50_THRESHOLD_MS, f"p50 {p50_ms:.1f}ms ≥ {_P50_THRESHOLD_MS}ms"
        assert p95_ms < _P95_THRESHOLD_MS, f"p95 {p95_ms:.1f}ms ≥ {_P95_THRESHOLD_MS}ms"

    def test_large_file_latency(self, server_url: str):
        url = f"{server_url}/file-5mb.bin"
        r = _measure_latency(url, n=10)
        p50_ms = r["p50"] * 1000
        p95_ms = r["p95"] * 1000
        p99_ms = r["p99"] * 1000
        print(
            f"\n  Large file (5MB) latency [n={r['n']}]:"
            f"\n    p50={p50_ms:.1f}ms  p95={p95_ms:.1f}ms"
            f"\n    p99={p99_ms:.1f}ms  mean={r['mean']*1000:.1f}ms"
            f"\n    min={r['min']*1000:.1f}ms  max={r['max']*1000:.1f}ms"
        )
        assert p50_ms < 50, f"p50 {p50_ms:.1f}ms ≥ 50ms"
        assert p95_ms < 100, f"p95 {p95_ms:.1f}ms ≥ 100ms"
        assert p99_ms < _P99_THRESHOLD_MS, f"p99 {p99_ms:.1f}ms ≥ {_P99_THRESHOLD_MS}ms"


class TestTTFB:
    def test_ttfb_small_file(self, server_url: str):
        url = f"{server_url}/file-1kb.bin"
        ttfb_values = []
        for _ in range(10):
            start = time.perf_counter()
            resp = urllib.request.urlopen(url)
            chunk = resp.read(1)
            ttfb_values.append(time.perf_counter() - start)
            resp.read()
        ttfb_values.sort()
        p50_ms = ttfb_values[len(ttfb_values) // 2] * 1000
        p95_ms = ttfb_values[int(len(ttfb_values) * 0.95)] * 1000
        print(
            f"\n  TTFB (1KB):"
            f"\n    p50={p50_ms:.1f}ms"
            f"\n    p95={p95_ms:.1f}ms"
        )
        assert p50_ms < 10, f"TTFB p50 {p50_ms:.1f}ms ≥ 10ms"


class TestGzipVsRaw:
    def test_gzip_vs_raw_comparison(self, server_url: str):
        raw_url = f"{server_url}/about.html"
        gzip_url = f"{server_url}/about.html"

        raw_times = []
        gzip_times = []
        for _ in range(15):
            start = time.perf_counter()
            resp = urllib.request.urlopen(raw_url)
            raw_len = len(resp.read())
            raw_times.append(time.perf_counter() - start)

            req = urllib.request.Request(gzip_url, headers={"Accept-Encoding": "gzip"})
            start = time.perf_counter()
            resp = urllib.request.urlopen(req)
            gzip_len = len(resp.read())
            gzip_times.append(time.perf_counter() - start)

        raw_p50 = sorted(raw_times)[len(raw_times) // 2]
        gzip_p50 = sorted(gzip_times)[len(gzip_times) // 2]

        print(
            f"\n  gzip vs raw (about.html):"
            f"\n    Raw:        p50={raw_p50*1000:.1f}ms  ({raw_len} bytes)"
            f"\n    gzip:       p50={gzip_p50*1000:.1f}ms  ({gzip_len} bytes)"
            f"\n    Compression ratio: {gzip_len/raw_len*100:.1f}%"
        )


class TestConcurrentThroughput:
    CONCURRENCY_LEVELS = [5, 25, 100]

    def test_throughput_small_file(self, server_url: str):
        url = f"{server_url}/file-1kb.bin"
        print(f"\n  Throughput — small file (1KB):")
        for c in self.CONCURRENCY_LEVELS:
            r = _measure_throughput(url, n_requests=100, n_concurrent=c)
            print(
                f"    concurrent={r['n_concurrent']:>3}  "
                f"{r['throughput_req_per_sec']:>8.0f} req/s  "
                f"p50={r['p50']*1000:>6.1f}ms  p95={r['p95']*1000:>6.1f}ms"
            )
            assert r["throughput_req_per_sec"] > 20, (
                f"Throughput {r['throughput_req_per_sec']} req/s ≤ 20 (concurrent={c})"
            )

    def test_throughput_medium_file(self, server_url: str):
        url = f"{server_url}/file-100kb.bin"
        print(f"\n  Throughput — medium file (100KB):")
        for c in self.CONCURRENCY_LEVELS:
            r = _measure_throughput(url, n_requests=50, n_concurrent=c)
            print(
                f"    concurrent={c:>3}  "
                f"{r['throughput_req_per_sec']:>8.0f} req/s  "
                f"p50={r['p50']*1000:>6.1f}ms  p95={r['p95']*1000:>6.1f}ms"
            )
            assert r["throughput_req_per_sec"] > 20, (
                f"Throughput {r['throughput_req_per_sec']} req/s ≤ 20 (concurrent={c})"
            )

    def test_throughput_large_file(self, server_url: str):
        url = f"{server_url}/file-5mb.bin"
        print(f"\n  Throughput — large file (5MB):")
        for c in [5, 10]:
            r = _measure_throughput(url, n_requests=20, n_concurrent=c)
            print(
                f"    concurrent={c:>3}  "
                f"{r['throughput_req_per_sec']:>8.2f} req/s  "
                f"p50={r['p50']*1000:>6.1f}ms  p95={r['p95']*1000:>6.1f}ms"
            )
            assert r["throughput_req_per_sec"] > 5, (
                f"Throughput {r['throughput_req_per_sec']} req/s ≤ 5 (concurrent={c})"
            )
