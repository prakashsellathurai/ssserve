from __future__ import annotations

import gzip
import hashlib
import html
import io
import mimetypes
import os
import re
import time
import urllib.parse
from email.utils import formatdate, parsedate
from fnmatch import fnmatch
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler

from ssserve.config import Config
from ssserve.listing import render_listing


def _route_to_regex(pattern: str) -> re.Pattern:
    parts = []
    for segment in pattern.split("/"):
        if segment.startswith(":"):
            parts.append(f"(?P<{segment[1:]}>[^/]+)")
        elif "*" in segment:
            parts.append(re.escape(segment).replace(r"\*\*", ".*").replace(r"\*", "[^/]*"))
        else:
            parts.append(re.escape(segment))
    return re.compile(f"^{'/'.join(parts)}$")


def _apply_segments(template: str, groups: dict[str, str]) -> str:
    result = template
    for key, val in groups.items():
        result = result.replace(f":{key}", val)
    return result


def _match_glob(path: str, pattern: str) -> bool:
    if pattern.startswith("!"):
        return not fnmatch(path, pattern[1:])
    return fnmatch(path, pattern)


def _match_glob_list(path: str, patterns: list[str]) -> bool:
    result = False
    for p in patterns:
        if p.startswith("!"):
            if fnmatch(path, p[1:]):
                return False
        else:
            if fnmatch(path, p):
                result = True
    return result


def _parse_byte_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    if not range_header or not range_header.startswith("bytes="):
        return None
    parts = range_header[6:].split("-", 1)
    if len(parts) != 2:
        return None
    start_str, end_str = parts
    try:
        if start_str == "":
            start = file_size - int(end_str)
            end = file_size - 1
        else:
            start = int(start_str)
            end = int(end_str) if end_str else file_size - 1
        if start < 0 or end >= file_size or start > end:
            return None
        return start, end
    except ValueError:
        return None


class ServeHandler(BaseHTTPRequestHandler):
    server_version = "ssserve/0.1.0"
    default_request_version = "HTTP/1.1"

    config: Config = Config.defaults()
    cors: bool = False
    single: bool = False
    debug: bool = False
    logging_enabled: bool = True
    no_compression: bool = False
    no_port_switching: bool = False
    root_dir: str = os.getcwd()

    def log_message(self, format: str, *args) -> None:
        if self.logging_enabled:
            super().log_message(format, *args)

    def _log_request(self, status: int, size: int = 0) -> None:
        if not self.logging_enabled:
            return
        self.log_message('"%s %s %s" %d %d', self.command, self.path, self.request_version, status, size)

    def _send_redirect(self, location: str, status: int = 301) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self._apply_common_headers()
        self.end_headers()
        self._log_request(status)

    def _send_error_page(self, status: int, message: str = "") -> None:
        status_code = int(status)
        error_page = os.path.join(self.root_dir, f"{status_code}.html")
        content = f"<h1>{status_code} {HTTPStatus(status_code).phrase}</h1>"
        if os.path.isfile(error_page):
            with open(error_page, "rb") as f:
                body = f.read()
            content = body.decode("utf-8", errors="replace")
        else:
            content = f"<!doctype html><html><head><meta charset='utf-8'><title>{status_code}</title><style>body{{font-family:sans-serif;padding:40px;text-align:center}}h1{{font-weight:400;color:#333}}p{{color:#666}}</style></head><body><h1>{status_code}</h1><p>{html.escape(message)}</p></body></html>"
            if status_code == 404:
                content = f"<!doctype html><html><head><meta charset='utf-8'><title>404 Not Found</title><style>body{{font-family:sans-serif;padding:40px;text-align:center}}h1{{font-weight:400;color:#333}}</style></head><body><h1>404</h1><p>Not Found</p></body></html>"

        body_bytes = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self._apply_common_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body_bytes)
        self._log_request(status, len(body_bytes))

    def _apply_common_headers(self) -> None:
        if self.cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Credentials", "true")

    def _get_mime(self, path: str) -> str:
        mime, _ = mimetypes.guess_type(path)
        return mime or "application/octet-stream"

    def _normalize_path(self, url_path: str) -> str:
        parsed = urllib.parse.urlsplit(url_path)
        path = urllib.parse.unquote(parsed.path)
        has_trailing_slash = path.endswith("/") and path != "/"
        path = os.path.normpath(path).replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path
        if has_trailing_slash and not path.endswith("/"):
            path += "/"
        return path

    def _resolve_path(self, url_path: str) -> str | None:
        normalized = self._normalize_path(url_path)
        if self.config.public:
            base = os.path.join(self.root_dir, self.config.public)
        else:
            base = self.root_dir
        fs_path = os.path.normpath(os.path.join(base, normalized.lstrip("/")))

        if not fs_path.startswith(os.path.normpath(base)):
            return None

        if not self.config.symlinks and os.path.islink(fs_path):
            return None

        if not os.path.exists(fs_path):
            return None

        return fs_path

    def _check_clean_urls(self, url_path: str) -> str | None:
        if isinstance(self.config.clean_urls, bool) and not self.config.clean_urls:
            return None
        if url_path.endswith(".html"):
            stripped = url_path[: -len(".html")] if url_path != "/index.html" else "/"
            if stripped == "":
                stripped = "/"
            fs_path = self._resolve_with_clean_urls(stripped)
            if fs_path:
                return stripped
            return None
        return None

    def _resolve_with_clean_urls(self, url_path: str) -> str | None:
        fs_path = self._resolve_path(url_path)
        if fs_path:
            return fs_path
        if self.config.clean_urls is not False:
            html_path = url_path.rstrip("/") + ".html"
            if url_path == "/":
                html_path = "/index.html"
            fs_path = self._resolve_path(html_path)
            if fs_path and os.path.isfile(fs_path):
                return fs_path
        return None

    def _check_trailing_slash(self, url_path: str) -> str | None:
        if self.config.trailing_slash is None:
            return None
        fs_path = self._resolve_path(url_path)
        if fs_path and os.path.isdir(fs_path):
            if not url_path.endswith("/") and self.config.trailing_slash:
                return url_path + "/"
            if url_path.endswith("/") and not self.config.trailing_slash:
                return url_path.rstrip("/") or "/"
        if fs_path and os.path.isfile(fs_path):
            if url_path.endswith("/") and not self.config.trailing_slash:
                return url_path.rstrip("/") or "/"
        return None

    def _check_redirects(self, url_path: str) -> tuple[str, int] | None:
        for rule in self.config.redirects:
            regex = _route_to_regex(rule.source)
            m = regex.match(url_path)
            if m:
                dest = _apply_segments(rule.destination, m.groupdict())
                return dest, rule.type
        return None

    def _check_rewrites(self, url_path: str) -> str | None:
        for rule in self.config.rewrites:
            regex = _route_to_regex(rule.source)
            m = regex.match(url_path)
            if m:
                return _apply_segments(rule.destination, m.groupdict())
        return None

    def _get_custom_headers(self, url_path: str) -> list[tuple[str, str | None]]:
        result = []
        for rule in self.config.headers:
            if _match_glob(url_path, rule.source):
                for h in rule.headers:
                    result.append((h["key"], h.get("value")))
        return result

    def _is_listing_allowed(self, url_path: str) -> bool:
        val = self.config.directory_listing
        if isinstance(val, bool):
            return val
        return _match_glob_list(url_path, val)

    def _is_unlisted(self, name: str) -> bool:
        return _match_glob_list(name, self.config.unlisted)

    def _serve_file(self, fs_path: str, url_path: str, byte_range: tuple[int, int] | None = None) -> None:
        try:
            stat = os.stat(fs_path)
            file_size = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            self._send_error_page(500)
            return

        mime = self._get_mime(fs_path)
        etag_val = None
        if self.config.etag:
            hasher = hashlib.sha256()
            try:
                with open(fs_path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        hasher.update(chunk)
                etag_val = f'"{hasher.hexdigest()}"'
            except OSError:
                etag_val = None

        if byte_range:
            status = HTTPStatus.PARTIAL_CONTENT
            start, end = byte_range
            content_length = end - start + 1
        else:
            status = HTTPStatus.OK
            content_length = file_size

            if etag_val:
                if_none_match = self.headers.get("If-None-Match")
                if if_none_match and if_none_match.strip('" ') == etag_val.strip('" '):
                    self.send_response(HTTPStatus.NOT_MODIFIED)
                    self._apply_common_headers()
                    if etag_val:
                        self.send_header("ETag", etag_val)
                    self.end_headers()
                    self._log_request(304)
                    return
            else:
                ims = self.headers.get("If-Modified-Since")
                if ims:
                    try:
                        ims_time = time.mktime(parsedate(ims))
                        if int(mtime) <= int(ims_time):
                            self.send_response(HTTPStatus.NOT_MODIFIED)
                            self._apply_common_headers()
                            self.end_headers()
                            self._log_request(304)
                            return
                    except (TypeError, OSError):
                        pass

        accept_encoding = self.headers.get("Accept-Encoding", "")
        use_gzip = (
            not self.no_compression
            and "gzip" in accept_encoding
        )

        gzipped_data = None
        if use_gzip and not byte_range:
            try:
                with open(fs_path, "rb") as f:
                    raw = f.read()
                gzipped_data = gzip.compress(raw)
                content_length = len(gzipped_data)
            except OSError:
                gzipped_data = None
        else:
            gzipped_data = None

        self.send_response(status)
        self.send_header("Content-Type", mime)
        if content_length is not None:
            self.send_header("Content-Length", str(content_length))
        if etag_val:
            self.send_header("ETag", etag_val)
        else:
            self.send_header("Last-Modified", formatdate(mtime, usegmt=True))
        if byte_range:
            self.send_header("Content-Range", f"bytes {byte_range[0]}-{byte_range[1]}/{file_size}")
        if not self.config.etag:
            self.send_header("Accept-Ranges", "bytes")
        if gzipped_data is not None:
            self.send_header("Content-Encoding", "gzip")

        for key, val in self._get_custom_headers(url_path):
            if val is None:
                self.send_header(key, "")
            else:
                self.send_header(key, val)

        self._apply_common_headers()
        self.end_headers()

        if self.command == "HEAD":
            self._log_request(status, file_size)
            return

        try:
            if gzipped_data is not None:
                self.wfile.write(gzipped_data)
            elif byte_range:
                with open(fs_path, "rb") as f:
                    f.seek(byte_range[0])
                    remaining = byte_range[1] - byte_range[0] + 1
                    while remaining > 0:
                        chunk_size = min(65536, remaining)
                        data = f.read(chunk_size)
                        if not data:
                            break
                        self.wfile.write(data)
                        remaining -= len(data)
            else:
                with open(fs_path, "rb") as f:
                    self.copyfile(f, self.wfile)
        except OSError:
            pass

        self._log_request(status, file_size)

    def _handle_request(self) -> None:
        url_path = self._normalize_path(self.path)

        if url_path != self.path:
            self._send_redirect(url_path, 301)
            return

        if self.command != "HEAD":
            cu_result = self._check_clean_urls(url_path)
            if cu_result:
                self._send_redirect(cu_result, 301)
                return

        ts_result = self._check_trailing_slash(url_path)
        if ts_result:
            self._send_redirect(ts_result, 301)
            return

        redirect = self._check_redirects(url_path)
        if redirect:
            dest, status = redirect
            self._send_redirect(dest, status)
            return

        rewritten = self._check_rewrites(url_path)
        if rewritten:
            url_path = rewritten

        fs_path = self._resolve_with_clean_urls(url_path)

        if fs_path and os.path.isdir(fs_path):
            for index in ("index.html", "index.htm"):
                index_path = os.path.join(fs_path, index)
                if os.path.isfile(index_path):
                    self._serve_file(index_path, url_path.rstrip("/") + "/" + index)
                    return

            if self.config.render_single:
                entries = [e for e in os.scandir(fs_path) if not self._is_unlisted(e.name)]
                if len(entries) == 1:
                    entry = entries[0]
                    file_path = os.path.join(fs_path, entry.name)
                    if entry.is_file():
                        sub_url = url_path.rstrip("/") + "/" + entry.name
                        self._serve_file(file_path, sub_url)
                        return

            if self._is_listing_allowed(url_path):
                try:
                    entries = sorted(os.scandir(fs_path), key=lambda e: (not e.is_dir(), e.name.lower()))
                    entries = [e for e in entries if not self._is_unlisted(e.name)]
                    if not url_path.endswith("/"):
                        self._send_redirect(url_path + "/", 301)
                        return
                    html = render_listing(url_path, fs_path, entries)
                    body = html.encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self._apply_common_headers()
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
                    self._log_request(200, len(body))
                    return
                except OSError:
                    pass

            self._send_error_page(404)
            return

        if fs_path and os.path.isfile(fs_path):
            range_header = self.headers.get("Range", "")
            byte_range = _parse_byte_range(range_header, os.path.getsize(fs_path))
            self._serve_file(fs_path, url_path, byte_range)
            return

        if self.single:
            index_path = os.path.join(self.root_dir, "index.html")
            if os.path.isfile(index_path):
                self._serve_file(index_path, "/index.html")
                return

        self._send_error_page(404)

    def do_GET(self) -> None:
        self._handle_request()

    def do_HEAD(self) -> None:
        self._handle_request()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._apply_common_headers()
        self.end_headers()

    def copyfile(self, source: io.IOBase, output: io.IOBase) -> None:
        buf = source.read(65536)
        while buf:
            output.write(buf)
            buf = source.read(65536)
