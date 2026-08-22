import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _populate_dir(base: str) -> None:
    b = base
    with open(os.path.join(b, "index.html"), "w") as f:
        f.write("<h1>Home</h1>")
    with open(os.path.join(b, "about.html"), "w") as f:
        f.write("<h1>About Us</h1>")
    with open(os.path.join(b, "style.css"), "w") as f:
        f.write("body { color: red; }")
    with open(os.path.join(b, "script.js"), "w") as f:
        f.write("console.log('test');")
    with open(os.path.join(b, "data.json"), "w") as f:
        f.write('{"key": "value"}')
    with open(os.path.join(b, "404.html"), "w") as f:
        f.write("<h1>Custom 404</h1>")
    with open(os.path.join(b, "file-1kb.bin"), "wb") as f:
        f.write(b"x" * 1024)
    with open(os.path.join(b, "file-100kb.bin"), "wb") as f:
        f.write(b"x" * (100 * 1024))
    with open(os.path.join(b, "file-5mb.bin"), "wb") as f:
        f.write(b"x" * (5 * 1024 * 1024))
    blog = os.path.join(b, "blog")
    os.makedirs(blog, exist_ok=True)
    with open(os.path.join(blog, "index.html"), "w") as f:
        f.write("<h1>Blog Home</h1>")
    with open(os.path.join(blog, "post.html"), "w") as f:
        f.write("<h1>Blog Post</h1>")
    dl = os.path.join(b, "downloads")
    os.makedirs(dl, exist_ok=True)
    with open(os.path.join(dl, "readme.txt"), "w") as f:
        f.write("hello world")


def _start_server(serve_dir: str, port: int) -> subprocess.Popen:
    venv_python = os.path.join(
        os.path.dirname(__file__), "..", ".venv", "bin", "python"
    )
    python = venv_python if os.path.exists(venv_python) else sys.executable
    args = [python, "-m", "ssserve", serve_dir, "-l", str(port), "--no-port-switching", "-L"]
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Server exited with code {proc.returncode}")
        try:
            with socket.create_connection(("localhost", port), timeout=0.5) as s:
                s.sendall(b"GET / HTTP/1.0\r\n\r\n")
                if b"HTTP/" in s.recv(128):
                    return proc
        except (ConnectionRefusedError, OSError, socket.timeout):
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError(f"Server did not start on port {port}")


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _fetch(url: str) -> bytes:
    resp = urllib.request.urlopen(url)
    return resp.read()


def _fetch_raw(url: str) -> float:
    start = time.perf_counter()
    resp = urllib.request.urlopen(url)
    resp.read()
    return time.perf_counter() - start


class BenchmarkStaticHTML:
    """HTML page serving - render-blocking, core web vitals."""

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        _populate_dir(self.tmpdir)
        self.port = _find_free_port()
        self.proc = _start_server(self.tmpdir, self.port)
        self.base = f"http://localhost:{self.port}"

    def teardown(self):
        _stop_server(self.proc)
        shutil.rmtree(self.tmpdir)

    def time_index_page(self):
        _fetch(f"{self.base}/index.html")

    def time_about_page(self):
        _fetch(f"{self.base}/about.html")

    def time_404_page(self):
        try:
            _fetch(f"{self.base}/nonexistent.html")
        except urllib.error.HTTPError:
            pass


class BenchmarkStaticAssets:
    """CSS/JS serving - parse-blocking resources."""

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        _populate_dir(self.tmpdir)
        self.port = _find_free_port()
        self.proc = _start_server(self.tmpdir, self.port)
        self.base = f"http://localhost:{self.port}"

    def teardown(self):
        _stop_server(self.proc)
        shutil.rmtree(self.tmpdir)

    def time_css_file(self):
        _fetch(f"{self.base}/style.css")

    def time_js_file(self):
        _fetch(f"{self.base}/script.js")

    def time_json_api(self):
        _fetch(f"{self.base}/data.json")


class BenchmarkBinaryServing:
    """Binary file serving - images, downloads, media."""

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        _populate_dir(self.tmpdir)
        self.port = _find_free_port()
        self.proc = _start_server(self.tmpdir, self.port)
        self.base = f"http://localhost:{self.port}"

    def teardown(self):
        _stop_server(self.proc)
        shutil.rmtree(self.tmpdir)

    def time_small_binary_1kb(self):
        _fetch(f"{self.base}/file-1kb.bin")

    def time_medium_binary_100kb(self):
        _fetch(f"{self.base}/file-100kb.bin")

    def time_large_binary_5mb(self):
        _fetch(f"{self.base}/file-5mb.bin")


class BenchmarkDirectoryListing:
    """Directory listing - dev server UX."""

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        _populate_dir(self.tmpdir)
        self.port = _find_free_port()
        self.proc = _start_server(self.tmpdir, self.port)
        self.base = f"http://localhost:{self.port}"

    def teardown(self):
        _stop_server(self.proc)
        shutil.rmtree(self.tmpdir)

    def time_root_listing(self):
        _fetch(f"{self.base}/")

    def time_subdirectory_listing(self):
        _fetch(f"{self.base}/blog/")

    def time_deep_listing(self):
        _fetch(f"{self.base}/downloads/")


class BenchmarkConcurrentThroughput:
    """High concurrency - spike traffic handling."""

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        _populate_dir(self.tmpdir)
        self.port = _find_free_port()
        self.proc = _start_server(self.tmpdir, self.port)
        self.base = f"http://localhost:{self.port}"

    def teardown(self):
        _stop_server(self.proc)
        shutil.rmtree(self.tmpdir)

    def time_10_concurrent_requests(self):
        url = f"{self.base}/file-1kb.bin"
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = [ex.submit(_fetch, url) for _ in range(50)]
            for f in as_completed(futures):
                f.result()

    def time_50_concurrent_requests(self):
        url = f"{self.base}/file-1kb.bin"
        with ThreadPoolExecutor(max_workers=50) as ex:
            futures = [ex.submit(_fetch, url) for _ in range(100)]
            for f in as_completed(futures):
                f.result()

    def time_mixed_concurrent_assets(self):
        urls = [
            f"{self.base}/index.html",
            f"{self.base}/style.css",
            f"{self.base}/script.js",
            f"{self.base}/data.json",
            f"{self.base}/file-1kb.bin",
        ]
        with ThreadPoolExecutor(max_workers=25) as ex:
            futures = [ex.submit(_fetch, urls[i % len(urls)]) for i in range(50)]
            for f in as_completed(futures):
                f.result()


class BenchmarkKeepAlive:
    """Connection reuse - sequential requests."""

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        _populate_dir(self.tmpdir)
        self.port = _find_free_port()
        self.proc = _start_server(self.tmpdir, self.port)
        self.host = "localhost"

    def teardown(self):
        _stop_server(self.proc)
        shutil.rmtree(self.tmpdir)

    def time_10_sequential_requests(self):
        conn = http.client.HTTPConnection(self.host, self.port)
        for _ in range(10):
            conn.request("GET", "/file-1kb.bin")
            resp = conn.getresponse()
            resp.read()
        conn.close()

    def time_50_sequential_requests(self):
        conn = http.client.HTTPConnection(self.host, self.port)
        for _ in range(50):
            conn.request("GET", "/file-1kb.bin")
            resp = conn.getresponse()
            resp.read()
        conn.close()


class BenchmarkGzipCompression:
    """Compression effectiveness - transfer size."""

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        _populate_dir(self.tmpdir)
        self.port = _find_free_port()
        self.proc = _start_server(self.tmpdir, self.port)
        self.base = f"http://localhost:{self.port}"

    def teardown(self):
        _stop_server(self.proc)
        shutil.rmtree(self.tmpdir)

    def time_gzip_html(self):
        req = urllib.request.Request(
            f"{self.base}/about.html",
            headers={"Accept-Encoding": "gzip"},
        )
        resp = urllib.request.urlopen(req)
        resp.read()

    def time_gzip_css(self):
        req = urllib.request.Request(
            f"{self.base}/style.css",
            headers={"Accept-Encoding": "gzip"},
        )
        resp = urllib.request.urlopen(req)
        resp.read()

    def time_gzip_js(self):
        req = urllib.request.Request(
            f"{self.base}/script.js",
            headers={"Accept-Encoding": "gzip"},
        )
        resp = urllib.request.urlopen(req)
        resp.read()


class BenchmarkTTFB:
    """Time to First Byte - server response latency."""

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        _populate_dir(self.tmpdir)
        self.port = _find_free_port()
        self.proc = _start_server(self.tmpdir, self.port)
        self.base = f"http://localhost:{self.port}"

    def teardown(self):
        _stop_server(self.proc)
        shutil.rmtree(self.tmpdir)

    def time_ttfb_html(self):
        start = time.perf_counter()
        resp = urllib.request.urlopen(f"{self.base}/index.html")
        resp.read(1)
        return time.perf_counter() - start

    def time_ttfb_binary(self):
        start = time.perf_counter()
        resp = urllib.request.urlopen(f"{self.base}/file-1kb.bin")
        resp.read(1)
        return time.perf_counter() - start

    def time_ttfb_404(self):
        start = time.perf_counter()
        try:
            resp = urllib.request.urlopen(f"{self.base}/nonexistent.html")
            resp.read(1)
        except urllib.error.HTTPError:
            pass
        return time.perf_counter() - start


class BenchmarkLargeDirectory:
    """Large directory listing - performance under scale."""

    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        for i in range(100):
            with open(os.path.join(self.tmpdir, f"file{i:03d}.html"), "w") as f:
                f.write(f"<h1>File {i}</h1>")
            with open(os.path.join(self.tmpdir, f"file{i:03d}.css"), "w") as f:
                f.write(f"body {{ color: {i % 2 and 'red' or 'blue'}; }}")
        self.port = _find_free_port()
        self.proc = _start_server(self.tmpdir, self.port)
        self.base = f"http://localhost:{self.port}"

    def teardown(self):
        _stop_server(self.proc)
        shutil.rmtree(self.tmpdir)

    def time_render_300_file_listing(self):
        _fetch(f"{self.base}/")

    def time_serve_individual_file(self):
        _fetch(f"{self.base}/file050.html")
