import os
import re
import tempfile

from ssserve.config import Config, merge_config
from ssserve.handler import _match_glob, _match_glob_list, _parse_byte_range, _route_to_regex
from ssserve.listing import format_date, format_size
from ssserve.network import parse_listen


class TimeRouteToRegex:
    def time_simple(self):
        _route_to_regex("/api/users")

    def time_with_param(self):
        _route_to_regex("/api/users/:id")

    def time_with_wildcard(self):
        _route_to_regex("/api/:resource/*")

    def time_complex(self):
        _route_to_regex("/api/:resource/:id/**")


class TimeMatchGlob:
    def time_simple(self):
        _match_glob("/api/users/123", "/api/users/*")

    def time_negative(self):
        _match_glob("/api/users/123", "!/api/users/*")

    def time_list(self):
        _match_glob_list("hidden.txt", [".git", ".DS_Store", "*.txt"])


class TimeParseByteRange:
    params = [["bytes=0-99", "bytes=100-199", "bytes=-100"]]

    def time_parse(self, header):
        _parse_byte_range(header, 1000)


class TimeFormatSize:
    params = [[0, 500, 2048, 1048576, 1073741824]]

    def time_format(self, size):
        format_size(size)


class TimeFormatDate:
    def time_format(self):
        format_date(1700000000.0)


class TimeParseListen:
    params = [["tcp://0.0.0.0:3000", "tcp://127.0.0.1:8080", "unix:/tmp/test.sock"]]

    def time_parse(self, value):
        parse_listen(value)


class TimeMergeConfig:
    def setup(self):
        self.overrides = {
            "public": "dist",
            "cleanUrls": True,
            "directoryListing": False,
            "rewrites": [
                {"source": "/api/*", "destination": "/api/$1"}
            ],
            "redirects": [
                {"source": "/old", "destination": "/new", "type": 301}
            ],
            "headers": [
                {"source": "**/*.js", "headers": [{"key": "Cache-Control", "value": "public,max-age=31536000,immutable"}]}
            ],
        }

    def time_merge(self):
        merge_config(Config.defaults(), self.overrides)


class TimeRenderListing:
    def setup(self):
        self.tmpdir = tempfile.mkdtemp()
        for i in range(100):
            name = f"file{i}.html"
            path = os.path.join(self.tmpdir, name)
            with open(path, "w") as f:
                f.write(f"<h1>{i}</h1>")
        self.entries = list(os.scandir(self.tmpdir))

    def teardown(self):
        for entry in self.entries:
            try:
                os.unlink(entry.path)
            except OSError:
                pass
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass

    def time_render(self):
        from ssserve.listing import render_listing

        render_listing("/", self.tmpdir, self.entries)
