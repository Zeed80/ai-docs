"""E33: the browser's only way out — an in-process egress proxy.

Chromium sends every request through this proxy, loopback included
(``--proxy-bypass-list=<-loopback>``): navigations, redirects, subresources,
XHR/fetch, WebSockets, downloads. The proxy resolves the host itself, refuses
if ANY resolved address is not allowed, and connects to the checked address,
so DNS rebinding between check and connect has nothing to rebind; alternate
IP spellings (decimal, hex, IPv4-mapped IPv6) resolve to the same address and
meet the same check. Only global unicast addresses are allowed; loopback,
private, link-local (cloud metadata), CGNAT, multicast and reserved ranges
are not, unless an explicit CIDR is listed in ``BROWSER_EGRESS_ALLOW_CIDRS``
(a deliberate per-deployment exception, not a bypass flag).

W2: a listener may have an *upstream* proxy (a residential or mobile exit the
operator configured). The check above still runs first and the upstream is
asked for the checked address, never for the name — the upstream cannot be
used to reach what this proxy refuses.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

_ALLOWED_PORTS = frozenset({80, 443, 8080, 8443})
_EXTRA_ALLOWED_PORTS = {
    int(p) for p in os.environ.get("BROWSER_EGRESS_ALLOW_PORTS", "").split(",") if p.strip()
}


def _allowed_networks() -> list[ipaddress._BaseNetwork]:
    raw = os.environ.get("BROWSER_EGRESS_ALLOW_CIDRS", "")
    return [ipaddress.ip_network(c.strip(), strict=False) for c in raw.split(",") if c.strip()]


ALLOW_NETWORKS = _allowed_networks()
blocked_log: list[tuple[str, int, str]] = []


def _explicitly_allowed(ip) -> bool:
    return any(ip in net for net in ALLOW_NETWORKS)


def _ip(address: str):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip


def address_allowed(address: str, port: int) -> bool:
    ip = _ip(address)
    if _explicitly_allowed(ip):
        return True
    return (
        ip.is_global
        and not ip.is_multicast
        and (port in _ALLOWED_PORTS or port in _EXTRA_ALLOWED_PORTS)
    )


async def resolve_checked(host: str, port: int) -> str:
    """The address to connect to, or PermissionError when any is forbidden."""
    host = host.strip("[]")
    if not host:
        raise PermissionError("no_host")
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise PermissionError("no_address")
    # Any forbidden answer refuses the host: a name that also points inside
    # is not trusted to point outside on the next lookup.
    if not all(address_allowed(a, port) for a in addresses):
        raise PermissionError("address_not_allowed")
    return addresses[0]


@dataclass(frozen=True)
class Upstream:
    """An HTTP proxy every connection of one listener goes through."""

    host: str
    port: int
    username: str = ""
    password: str = ""

    @classmethod
    def parse(cls, server: str, username: str = "", password: str = "") -> Upstream:
        parts = urlsplit(server if "://" in server else f"http://{server}")
        if parts.scheme not in ("http", "") or not parts.hostname:
            raise ValueError("upstream proxy must be http://host:port")
        return cls(
            host=parts.hostname,
            port=parts.port or 8080,
            username=username or unquote(parts.username or ""),
            password=password or unquote(parts.password or ""),
        )

    def auth_header(self) -> bytes:
        if not self.username:
            return b""
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return f"Proxy-Authorization: Basic {token}\r\n".encode()


async def _upstream_tunnel(upstream: Upstream, address: str, port: int):
    """A CONNECT tunnel through the upstream to the already checked address."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(upstream.host, upstream.port), timeout=20
    )
    target = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
    writer.write(
        f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n".encode()
        + upstream.auth_header()
        + b"\r\n"
    )
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
    status = head.split(b" ", 2)[1:2]
    if status != [b"200"]:
        writer.close()
        raise PermissionError(f"upstream_refused:{head[:40].decode('latin-1', 'replace')}")
    return reader, writer


async def _connect(address: str, port: int, upstream: Upstream | None):
    if upstream is None:
        return await asyncio.open_connection(address, port)
    return await _upstream_tunnel(upstream, address, port)


MAX_RESPONSE_BYTES = int(
    os.environ.get("BROWSER_EGRESS_MAX_RESPONSE_BYTES", str(100 * 1024 * 1024))
)


async def _pipe(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, limit: int | None = None
) -> None:
    sent = 0
    try:
        while data := await reader.read(65536):
            sent += len(data)
            if limit is not None and sent > limit:
                # E35: an endless or oversized response is cut, not stored.
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _refuse(writer: asyncio.StreamWriter, host: str, port: int, reason: str) -> None:
    blocked_log.append((host, port, reason))
    del blocked_log[:-200]
    body = f"blocked by browser egress policy: {reason}".encode()
    writer.write(
        b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    try:
        await writer.drain()
    finally:
        writer.close()


async def _handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream: Upstream | None = None,
):
    try:
        head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=30)
    except Exception:  # noqa: BLE001
        client_writer.close()
        return
    request_line, _, rest = head.partition(b"\r\n")
    try:
        method, target, version = request_line.decode("latin-1").split(" ", 2)
    except ValueError:
        client_writer.close()
        return

    if method.upper() == "CONNECT":
        host, _, port_text = target.rpartition(":")
        port = int(port_text or 443)
        try:
            address = await resolve_checked(host, port)
            upstream_reader, upstream_writer = await _connect(address, port, upstream)
        except Exception as exc:  # noqa: BLE001
            await _refuse(client_writer, host, port, str(exc)[:100])
            return
        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()
    else:
        parts = urlsplit(target)
        if parts.scheme != "http" or not parts.hostname:
            await _refuse(client_writer, target[:100], 0, "scheme_not_allowed")
            return
        host, port = parts.hostname, parts.port or 80
        try:
            address = await resolve_checked(host, port)
            upstream_reader, upstream_writer = await _connect(address, port, upstream)
        except Exception as exc:  # noqa: BLE001
            await _refuse(client_writer, host, port, str(exc)[:100])
            return
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        # One host per connection: the next request may go elsewhere and must
        # pass the check again, so the upstream connection is not reused.
        headers = [
            line
            for line in rest.split(b"\r\n")
            if line and not line.lower().startswith((b"proxy-", b"connection:"))
        ]
        length = 0
        for line in headers:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip() or 0)
            if name.strip().lower() == b"transfer-encoding":
                await _refuse(client_writer, host, port, "chunked_request_body")
                upstream_writer.close()
                return
        upstream_writer.write(
            f"{method} {path} {version}\r\n".encode("latin-1")
            + b"\r\n".join(headers)
            + b"\r\nConnection: close\r\n\r\n"
        )
        # Only this request's body goes up: the client may reuse its proxy
        # connection for another host, and that request must not reach this one.
        if length:
            upstream_writer.write(await client_reader.readexactly(length))
        await upstream_writer.drain()
        await _pipe(upstream_reader, client_writer, MAX_RESPONSE_BYTES)
        return

    await asyncio.gather(
        _pipe(client_reader, upstream_writer),
        _pipe(upstream_reader, client_writer, MAX_RESPONSE_BYTES),
    )


PROXY_HOST = os.environ.get("BROWSER_EGRESS_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("BROWSER_EGRESS_PROXY_PORT", "0"))


async def start_proxy(
    host: str | None = None, port: int | None = None, upstream: Upstream | None = None
) -> tuple[asyncio.AbstractServer, int]:
    """Listen for Chromium and, when configured, for the backend's workers.

    In the stack the proxy listens on the container network (port 8094): it is
    the one egress point for URLs a model chose — the browser and the
    workers' direct downloads (catalog files, robots.txt) alike. A listener
    with an upstream is loopback-only, for one browser profile.
    """

    async def handler(reader, writer):
        await _handle(reader, writer, upstream)

    server = await asyncio.start_server(
        handler,
        PROXY_HOST if host is None else host,
        PROXY_PORT if port is None else port,
    )
    return server, server.sockets[0].getsockname()[1]
