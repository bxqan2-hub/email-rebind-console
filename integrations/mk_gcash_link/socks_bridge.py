"""Loopback HTTP CONNECT bridge for authenticated SOCKS5 upstreams.

Chromium accepts username/password on HTTP proxy contexts but rejects SOCKS5
proxy authentication.  The bridge keeps the upstream credentials in memory,
listens only on loopback, and translates each CONNECT tunnel to SOCKS5.
"""
from __future__ import annotations

import atexit
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit


_CONNECT_TIMEOUT = 20
_RELAY_POLL = 60


def _read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        part = sock.recv(size - len(chunks))
        if not part:
            raise OSError("SOCKS5 upstream closed the connection")
        chunks.extend(part)
    return bytes(chunks)


def _socks5_connect(proxy_host, proxy_port, username, password, target_host, target_port):
    upstream = socket.create_connection((proxy_host, int(proxy_port)), timeout=_CONNECT_TIMEOUT)
    try:
        upstream.settimeout(_CONNECT_TIMEOUT)
        upstream.sendall(b"\x05\x01\x02")
        version, method = _read_exact(upstream, 2)
        if version != 5 or method == 0xFF:
            raise OSError("SOCKS5 authentication method rejected")
        if method == 0x02:
            user = str(username or "").encode("utf-8")
            secret = str(password or "").encode("utf-8")
            if len(user) > 255 or len(secret) > 255:
                raise OSError("SOCKS5 credentials are too long")
            upstream.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
            auth_version, auth_status = _read_exact(upstream, 2)
            if auth_version != 1 or auth_status != 0:
                raise OSError("SOCKS5 authentication failed")
        elif method != 0x00:
            raise OSError("SOCKS5 upstream selected an unsupported method")

        host = str(target_host).encode("idna")
        if len(host) > 255:
            raise OSError("SOCKS5 target host is too long")
        upstream.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(host)])
            + host
            + int(target_port).to_bytes(2, "big")
        )
        version, reply, _reserved, address_type = _read_exact(upstream, 4)
        if address_type == 1:
            _read_exact(upstream, 4)
        elif address_type == 3:
            length = _read_exact(upstream, 1)[0]
            _read_exact(upstream, length)
        elif address_type == 4:
            _read_exact(upstream, 16)
        else:
            raise OSError("SOCKS5 upstream returned an invalid address type")
        _read_exact(upstream, 2)
        if version != 5 or reply != 0:
            raise OSError(f"SOCKS5 CONNECT failed (reply={reply})")
        upstream.settimeout(None)
        return upstream
    except Exception:
        upstream.close()
        raise


def _relay(left: socket.socket, right: socket.socket) -> None:
    sockets = [left, right]
    try:
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, _RELAY_POLL)
            if exceptional:
                return
            if not readable:
                continue
            for source in readable:
                data = source.recv(64 * 1024)
                if not data:
                    return
                destination = right if source is left else left
                destination.sendall(data)
    except (ConnectionError, OSError):
        return


class _ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        upstream = None
        try:
            client.settimeout(_CONNECT_TIMEOUT)
            header_block = bytearray()
            while b"\r\n\r\n" not in header_block and len(header_block) <= 64 * 1024:
                part = client.recv(4096)
                if not part:
                    return
                header_block.extend(part)
            first_line = bytes(header_block).split(b"\r\n", 1)[0].decode("latin1", "replace")
            method, target, _version = first_line.split(" ", 2)
            if method.upper() != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                return
            parsed = urlsplit("//" + target)
            if not parsed.hostname or not parsed.port:
                raise OSError("invalid CONNECT target")
            bridge = self.server.bridge
            upstream = _socks5_connect(
                bridge.proxy_host,
                bridge.proxy_port,
                bridge.username,
                bridge.password,
                parsed.hostname,
                parsed.port,
            )
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            client.settimeout(None)
            _relay(client, upstream)
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
        finally:
            if upstream is not None:
                upstream.close()
            client.close()


class _BridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, bridge):
        super().__init__(("127.0.0.1", 0), _ConnectHandler)
        self.bridge = bridge


class Socks5HttpBridge:
    def __init__(self, proxy_host, proxy_port, username, password):
        self.proxy_host = str(proxy_host)
        self.proxy_port = int(proxy_port)
        self.username = str(username or "")
        self.password = str(password or "")
        self.server = _BridgeServer(self)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="gcash-socks5-http-bridge",
            daemon=True,
        )
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        server, thread = self.server, self.thread
        self.server = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread.is_alive():
            thread.join(timeout=2)


_LOCK = threading.Lock()
_BRIDGES: dict[tuple[str, int, str, str], Socks5HttpBridge] = {}


def get_bridge(proxy_host, proxy_port, username, password):
    key = (str(proxy_host), int(proxy_port), str(username or ""), str(password or ""))
    with _LOCK:
        bridge = _BRIDGES.get(key)
        if bridge is None or bridge.server is None:
            bridge = Socks5HttpBridge(*key)
            _BRIDGES[key] = bridge
        return bridge


def close_all():
    with _LOCK:
        bridges = list(_BRIDGES.values())
        _BRIDGES.clear()
    for bridge in bridges:
        bridge.close()


atexit.register(close_all)
