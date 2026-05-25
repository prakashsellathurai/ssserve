import os
import socket
import struct
from dataclasses import dataclass


@dataclass
class Address:
    scheme: str
    host: str
    port: int
    path: str | None

    def __str__(self) -> str:
        if self.scheme == "unix":
            return f"unix:{self.path}"
        host = self.host or "0.0.0.0"
        if ":" in host:
            host = f"[{host}]"
        return f"{host}:{self.port}"


def parse_listen(value: str) -> Address:
    value = value.strip()

    if value.startswith("unix:"):
        return Address(scheme="unix", host="", port=0, path=value[5:])

    if value.startswith("tcp://"):
        rest = value[6:]
        if rest.startswith("["):
            bracket_end = rest.find("]")
            if bracket_end == -1:
                raise ValueError(f"Invalid IPv6 address in listen URI: {value}")
            host = rest[1:bracket_end]
            after = rest[bracket_end + 1 :]
            if after.startswith(":"):
                port = int(after[1:])
            else:
                port = 3000
        elif ":" in rest:
            host, port_str = rest.rsplit(":", 1)
            port = int(port_str)
        else:
            host = rest
            port = 3000
        return Address(scheme="tcp", host=host, port=port, path=None)

    if value.startswith("pipe:"):
        raise ValueError("Windows named pipes are not supported")

    try:
        port = int(value)
        return Address(scheme="tcp", host="", port=port, path=None)
    except ValueError:
        pass

    if ":" in value:
        host, port_str = value.rsplit(":", 1)
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError(f"Invalid listen URI: {value}")
        return Address(scheme="tcp", host=host, port=port, path=None)

    return Address(scheme="tcp", host=value, port=3000, path=None)


def find_free_port(start: int = 3000, max_attempts: int = 100) -> int:
    port = start
    for _ in range(max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("", port))
                return port
            except OSError:
                port += 1
    raise RuntimeError("Could not find a free port")


def get_lan_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        return ip
    except Exception:
        pass
    finally:
        s.close()

    try:
        hostname = socket.gethostname()
        return socket.gethostbyname(hostname)
    except Exception:
        return None
