from __future__ import annotations

import os
import socket
import ssl
import socketserver
import sys
import time
from http.server import HTTPServer

import click

from ssserve import __version__
from ssserve.config import load_config
from ssserve.handler import ServeHandler
from ssserve.network import Address, find_free_port, get_lan_ip, parse_listen


def _create_server(
    addr: Address,
    handler_class: type,
    ssl_cert: str | None = None,
    ssl_key: str | None = None,
    ssl_pass: str | None = None,
) -> HTTPServer:
    if addr.scheme == "unix":
        if os.path.exists(addr.path):
            os.unlink(addr.path)
        server = HTTPServer(addr.path, handler_class)
        server.server_address = addr.path
    else:
        host = addr.host or "0.0.0.0"
        server = HTTPServer((host, addr.port), handler_class)
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
@click.option("-u", "--no-compression", is_flag=True, help="Disable compression")
@click.option("--no-etag", is_flag=True, help="Disable ETag (use Last-Modified)")
@click.option("-S", "--symlinks", is_flag=True, help="Resolve symlinks")
@click.option("--ssl-cert", type=click.Path(exists=True, dir_okay=False), help="SSL/TLS certificate (PEM)")
@click.option("--ssl-key", type=click.Path(exists=True, dir_okay=False), help="SSL/TLS private key (PEM)")
@click.option("--ssl-pass", type=click.Path(exists=True, dir_okay=False), help="SSL/TLS passphrase file")
@click.option("--no-port-switching", is_flag=True, help="Don't switch to another port when port is taken")
@click.version_option(version=__version__, prog_name="ssserve")
def main(
    path: str,
    listen: tuple[str, ...],
    single: bool,
    debug: bool,
    config: str | None,
    no_request_logging: bool,
    cors: bool,
    no_compression: bool,
    no_etag: bool,
    symlinks: bool,
    ssl_cert: str | None,
    ssl_key: str | None,
    ssl_pass: str | None,
    no_port_switching: bool,
) -> None:
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
    ServeHandler.single = single
    ServeHandler.debug = debug
    ServeHandler.logging_enabled = not no_request_logging
    ServeHandler.no_compression = no_compression
    ServeHandler.no_port_switching = no_port_switching
    ServeHandler.root_dir = root_dir

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

    if len(listeners) == 1:
        addr, port_switched = listeners[0]
        _print_startup(addr, cors, ssl_active, no_port_switching, no_compression, port_switched)
        server = _create_server(addr, ServeHandler, ssl_cert, ssl_key, ssl_pass)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            click.echo("\n  Shutting down...")
            server.shutdown()
    else:
        servers = []
        for addr, port_switched in listeners:
            _print_startup(addr, cors, ssl_active, no_port_switching, no_compression, port_switched)
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
