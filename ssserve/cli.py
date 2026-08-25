from __future__ import annotations

import cProfile
import atexit
import os
import pstats
import re
import signal
import socket
import ssl
import socketserver
import sys
import time
from http.server import HTTPServer
from socketserver import ThreadingMixIn

import click

_profiler: cProfile.Profile | None = None
_profile_path: str | None = None

from ssserve import __version__
from ssserve.config import load_config
from ssserve.handler import ServeHandler
from ssserve.livereload import LiveReload
from ssserve.network import Address, find_free_port, get_lan_ip, parse_listen


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


def _make_config_callback(cfg):
    """Create a config callback for the C server from a Config object."""
    redirects = []
    for rule in cfg.redirects:
        redirects.append({
            "source": rule.source,
            "destination": rule.destination,
            "type": rule.type,
            "compiled_regex": rule.compiled_regex,
        })

    rewrites = []
    for rule in cfg.rewrites:
        rewrites.append({
            "source": rule.source,
            "destination": rule.destination,
            "compiled_regex": rule.compiled_regex,
        })

    def callback(path: str):
        # Check redirects
        for rule in redirects:
            m = rule["compiled_regex"].match(path)
            if m:
                dest = _apply_segments(rule["destination"], m.groupdict())
                return dest, rule["type"]

        # Check rewrites
        for rule in rewrites:
            m = rule["compiled_regex"].match(path)
            if m:
                dest = _apply_segments(rule["destination"], m.groupdict())
                return dest, 200

        return None

    # Only return callback if there are rules to check
    if redirects or rewrites:
        return callback
    return None


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def _dump_profile() -> None:
    if _profiler and _profile_path:
        _profiler.disable()
        _profiler.dump_stats(_profile_path)


def _handle_sigterm(signum, frame):
    _dump_profile()
    os._exit(0)


def _create_server(
    addr: Address,
    handler_class: type,
    ssl_cert: str | None = None,
    ssl_key: str | None = None,
    ssl_pass: str | None = None,
) -> ThreadingHTTPServer:
    if addr.scheme == "unix":
        if os.path.exists(addr.path):
            os.unlink(addr.path)
        server = ThreadingHTTPServer(addr.path, handler_class)
        server.server_address = addr.path
    else:
        host = addr.host or "0.0.0.0"
        server = ThreadingHTTPServer((host, addr.port), handler_class)
        server.server_address = (host, addr.port)

    if ssl_cert and ssl_key:
        passphrase = None
        if ssl_pass:
            with open(ssl_pass) as f:
                passphrase = f.read().strip()
        ctx = ssl.SSLContext(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(ssl_cert, ssl_key, passphrase if passphrase else None)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    return server


def _print_startup(
    addr: Address,
    cors: bool,
    caching: bool,
    ssl_active: bool,
    no_port_switching: bool,
    no_compression: bool,
    port_switched: bool = False,
) -> None:
    scheme = "https" if ssl_active else "http"
    host = addr.host or "0.0.0.0"

    click.echo("")
    click.echo(f"  ssserve v{__version__}")
    click.echo("")

    if addr.scheme == "unix":
        click.echo(f"  ➜ Local:   unix:{addr.path}")
    else:
        local_url = f"  ➜ Local:   {scheme}://localhost:{addr.port}"
        if port_switched:
            local_url += f" (port {addr.port} was in use, switched)"
        click.echo(local_url)

        lan_ip = get_lan_ip()
        if lan_ip:
            click.echo(f"  ➜ Network: {scheme}://{lan_ip}:{addr.port}")

    click.echo("")

    if cors:
        click.echo("  ➜ CORS enabled")
    if not caching:
        click.echo("  ➜ Browser caching disabled")
    if ssl_active:
        click.echo("  ➜ SSL enabled")
    if not no_compression:
        click.echo("  ➜ Compression enabled (gzip)")

    click.echo("")


@click.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, dir_okay=True), default=".", required=False)
@click.option("-l", "--listen", type=str, multiple=True, default=["tcp://0.0.0.0:3000"], help="Listen endpoint")
@click.option("-s", "--single", is_flag=True, help="Single page application mode (rewrite 404 to index.html)")
@click.option("-d", "--debug", is_flag=True, help="Show debugging information")
@click.option("-c", "--config", type=click.Path(exists=True, dir_okay=False), help="Path to serve.json config")
@click.option("-L", "--no-request-logging", is_flag=True, help="Disable request logging")
@click.option("-C", "--cors", is_flag=True, help="Enable CORS")
@click.option("--caching", is_flag=True, help="Enable browser caching (disabled by default)")
@click.option("-u", "--no-compression", is_flag=True, help="Disable compression")
@click.option("--no-etag", is_flag=True, help="Disable ETag (use Last-Modified)")
@click.option("-S", "--symlinks", is_flag=True, help="Resolve symlinks")
@click.option("--ssl-cert", type=click.Path(exists=True, dir_okay=False), help="SSL/TLS certificate (PEM)")
@click.option("--ssl-key", type=click.Path(exists=True, dir_okay=False), help="SSL/TLS private key (PEM)")
@click.option("--ssl-pass", type=click.Path(exists=True, dir_okay=False), help="SSL/TLS passphrase file")
@click.option("--no-port-switching", is_flag=True, help="Don't switch to another port when port is taken")
@click.option("-r", "--live-reload", is_flag=True, help="Enable live reload on file changes")
@click.option("--profile", type=str, default=None, help="Dump cProfile stats to this file on shutdown")
@click.option("--python-server", is_flag=True, help="Use Python HTTP server instead of C server")
@click.version_option(version=__version__, prog_name="ssserve")
def main(
    path: str,
    listen: tuple[str, ...],
    single: bool,
    debug: bool,
    config: str | None,
    no_request_logging: bool,
    cors: bool,
    caching: bool,
    no_compression: bool,
    no_etag: bool,
    symlinks: bool,
    ssl_cert: str | None,
    ssl_key: str | None,
    ssl_pass: str | None,
    no_port_switching: bool,
    live_reload: bool,
    profile: str | None,
    python_server: bool,
) -> None:
    global _profiler, _profile_path
    if profile:
        _profiler = cProfile.Profile()
        _profile_path = profile
        _profiler.enable()
        atexit.register(_dump_profile)
        signal.signal(signal.SIGTERM, _handle_sigterm)
    root_dir = os.path.abspath(path) if path else os.getcwd()

    if not os.path.isdir(root_dir):
        click.echo(f"Error: {path} is not a directory", err=True)
        sys.exit(1)

    cfg = load_config(config, root_dir)

    if ssl_cert and not ssl_key:
        click.echo("Error: --ssl-key is required when --ssl-cert is provided", err=True)
        sys.exit(1)

    if no_etag:
        cfg.etag = False

    if symlinks:
        cfg.symlinks = True

    ServeHandler.config = cfg
    ServeHandler.cors = cors
    ServeHandler.caching = caching
    ServeHandler.single = single
    ServeHandler.debug = debug
    ServeHandler.logging_enabled = not no_request_logging
    ServeHandler.no_compression = no_compression
    ServeHandler.no_port_switching = no_port_switching
    ServeHandler.root_dir = root_dir

    lr: LiveReload | None = None
    if live_reload:
        lr = LiveReload(root_dir)
        lr.start()
        ServeHandler.live_reload = lr
        click.echo("  ➜ Live reload enabled")

    listeners = []
    for listen_val in listen:
        addr = parse_listen(listen_val)
        port_switched = False

        if addr.scheme == "tcp" and not no_port_switching:
            try:
                with socket.create_connection(("localhost", addr.port), timeout=0.5):
                    new_port = find_free_port(addr.port + 1)
                    click.echo(f"  Port {addr.port} is in use, using port {new_port} instead", err=True)
                    addr.port = new_port
                    port_switched = True
            except (ConnectionRefusedError, OSError, socket.timeout):
                pass

        listeners.append((addr, port_switched))

    ssl_active = ssl_cert is not None and ssl_key is not None

    if not python_server:
        if len(listeners) > 1:
            click.echo("  Warning: C server does not support multiple listeners, using Python server", err=True)
            python_server = True
        else:
            try:
                from ssserve._server import serve
                click.echo("  Using C server (epoll + thread pool)")
                if lr:
                    click.echo("  ➜ Live reload enabled")
                config_callback = _make_config_callback(cfg)
                custom_headers_list = []
                for rule in cfg.headers:
                    for h in rule.headers:
                        custom_headers_list.append((rule.source, h["key"], h.get("value", "")))
                for addr, port_switched in listeners:
                    _print_startup(addr, cors, caching, ssl_active, no_port_switching, no_compression, port_switched)
                    serve(
                        port=addr.port,
                        root_dir=root_dir,
                        cors=cors,
                        caching=caching,
                        etag=cfg.etag,
                        no_compression=no_compression,
                        symlinks=symlinks,
                        config_callback=config_callback,
                        custom_headers=custom_headers_list if custom_headers_list else None,
                        clean_urls=1 if cfg.clean_urls else 0,
                        trailing_slash=-1 if cfg.trailing_slash is None else (1 if cfg.trailing_slash else 0),
                        single=1 if single else 0,
                        live_reload=lr,
                    )
            except ImportError:
                click.echo("  Warning: C server not available, using Python server", err=True)
                python_server = True

    if python_server:
        if len(listeners) == 1:
            addr, port_switched = listeners[0]
            _print_startup(addr, cors, caching, ssl_active, no_port_switching, no_compression, port_switched)
            server = _create_server(addr, ServeHandler, ssl_cert, ssl_key, ssl_pass)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                click.echo("\n  Shutting down...")
                server.shutdown()
        else:
            servers = []
            for addr, port_switched in listeners:
                _print_startup(addr, cors, caching, ssl_active, no_port_switching, no_compression, port_switched)
                server = _create_server(addr, ServeHandler, ssl_cert, ssl_key, ssl_pass)
                servers.append(server)

            click.echo(f"  Serving {len(servers)} listeners")
            click.echo("")

            try:
                for server in servers:
                    import threading
                    t = threading.Thread(target=server.serve_forever, daemon=True)
                    t.start()
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                click.echo("\n  Shutting down...")
                for server in servers:
                    server.shutdown()


if __name__ == "__main__":
    main()
