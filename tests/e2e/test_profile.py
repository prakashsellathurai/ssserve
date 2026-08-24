from __future__ import annotations

import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest


@pytest.mark.profile
class TestCPUProfile:
    def test_cpu_profile_small_file(self, profiled_server: tuple[str, Path]):
        url, profiles_dir = profiled_server
        for _ in range(50):
            resp = urllib.request.urlopen(f"{url}/file-1kb.bin")
            resp.read()

    def test_cpu_profile_medium_file(self, profiled_server: tuple[str, Path]):
        url, profiles_dir = profiled_server
        for _ in range(20):
            resp = urllib.request.urlopen(f"{url}/file-100kb.bin")
            resp.read()

    def test_cpu_profile_concurrent(self, profiled_server: tuple[str, Path]):
        url, profiles_dir = profiled_server
        with ThreadPoolExecutor(max_workers=10) as ex:
            def _req(_):
                resp = urllib.request.urlopen(f"{url}/file-1kb.bin")
                resp.read()

            futures = [ex.submit(_req, i) for i in range(100)]
            for f in as_completed(futures):
                f.result()

    def test_cpu_profile_directory_listing(self, profiled_server: tuple[str, Path]):
        url, profiles_dir = profiled_server
        for _ in range(20):
            resp = urllib.request.urlopen(f"{url}/")
            resp.read()


@pytest.mark.profile
class TestMemoryProfile:
    def test_memory_allocations(self, memray_server: tuple[str, Path]):
        url, profiles_dir = memray_server
        for _ in range(50):
            resp = urllib.request.urlopen(f"{url}/file-1kb.bin")
            resp.read()

    def test_memory_large_file(self, memray_server: tuple[str, Path]):
        url, profiles_dir = memray_server
        for _ in range(10):
            resp = urllib.request.urlopen(f"{url}/file-5mb.bin")
            resp.read()

    def test_memory_concurrent_load(self, memray_server: tuple[str, Path]):
        url, profiles_dir = memray_server
        with ThreadPoolExecutor(max_workers=10) as ex:
            def _req(_):
                resp = urllib.request.urlopen(f"{url}/file-100kb.bin")
                resp.read()

            futures = [ex.submit(_req, i) for i in range(50)]
            for f in as_completed(futures):
                f.result()

    def test_memory_directory_listing(self, memray_server: tuple[str, Path]):
        url, profiles_dir = memray_server
        for _ in range(20):
            resp = urllib.request.urlopen(f"{url}/")
            resp.read()


@pytest.mark.profile
class TestSamplingProfile:
    def test_flame_graph_small_file(self, sampled_server: tuple[str, Path]):
        url, profiles_dir = sampled_server
        for _ in range(50):
            resp = urllib.request.urlopen(f"{url}/file-1kb.bin")
            resp.read()

    def test_flame_graph_concurrent(self, sampled_server: tuple[str, Path]):
        url, profiles_dir = sampled_server
        with ThreadPoolExecutor(max_workers=10) as ex:
            def _req(_):
                resp = urllib.request.urlopen(f"{url}/file-1kb.bin")
                resp.read()

            futures = [ex.submit(_req, i) for i in range(100)]
            for f in as_completed(futures):
                f.result()

    def test_flame_graph_directory_listing(self, sampled_server: tuple[str, Path]):
        url, profiles_dir = sampled_server
        for _ in range(20):
            resp = urllib.request.urlopen(f"{url}/")
            resp.read()
