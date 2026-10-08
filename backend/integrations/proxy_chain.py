"""Minimal HTTP CONNECT proxy chain: local proxy -> account proxy -> target."""

from __future__ import annotations

import base64
import select
import socket
import ssl
import threading
from urllib.parse import unquote, urlsplit


class ProxyChainError(RuntimeError):
    pass


def _parse_proxy(value: str):
    raw = str(value or "").strip()
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ProxyChainError("代理链只支持 HTTP/HTTPS 首跳和上游代理")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ProxyChainError(f"代理端口无效: {exc}") from exc
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ProxyChainError("代理地址不能包含路径、查询参数或片段")
    username = unquote(parsed.username or "") if parsed.username is not None else ""
    password = unquote(parsed.password or "") if parsed.password is not None else ""
    return parsed.scheme.lower(), parsed.hostname, port, username, password


def _authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"


def _proxy_auth(username: str, password: str) -> str:
    if username == "" and password == "":
        return ""
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Proxy-Authorization: Basic {token}\r\n"


def _read_headers(sock: socket.socket, limit: int = 64 * 1024) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise ProxyChainError("代理连接在响应头前关闭")
        data.extend(chunk)
        if len(data) > limit:
            raise ProxyChainError("代理响应头过大")
    return bytes(data)


def _status_code(response: bytes) -> int:
    first = response.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
    parts = first.split()
    if len(parts) < 2 or not parts[1].isdigit():
        raise ProxyChainError(f"代理响应无效: {first[:120]}")
    return int(parts[1])


def _connect_via_proxy(sock: socket.socket, proxy, target_host: str, target_port: int) -> None:
    _, _, _, username, password = proxy
    authority = _authority(target_host, target_port)
    request = (
        f"CONNECT {authority} HTTP/1.1\r\n"
        f"Host: {authority}\r\n"
        "Proxy-Connection: Keep-Alive\r\n"
        f"{_proxy_auth(username, password)}\r\n"
    ).encode("ascii")
    sock.sendall(request)
    code = _status_code(_read_headers(sock))
    if not 200 <= code < 300:
        raise ProxyChainError(f"上游代理 CONNECT {authority} 失败 HTTP {code}")


class ProxyChainBridge:
    """A short-lived local CONNECT endpoint for one account proxy lease."""

    def __init__(self, first_hop: str, second_hop: str, *, host: str = "127.0.0.1"):
        self.first_hop = str(first_hop or "").strip()
        self.second_hop = str(second_hop or "").strip()
        self.host = host
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self._url = ""

    @property
    def url(self) -> str:
        return self._url

    def start(self) -> str:
        if self._listener is not None:
            return self._url
        first = _parse_proxy(self.first_hop)
        second = _parse_proxy(self.second_hop)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, 0))
        listener.listen(32)
        listener.settimeout(0.5)
        self._listener = listener
        self._url = f"http://{self.host}:{listener.getsockname()[1]}"
        self._thread = threading.Thread(
            target=self._accept_loop,
            args=(first, second),
            name="proxy-chain-bridge",
            daemon=True,
        )
        self._thread.start()
        return self._url

    def _accept_loop(self, first, second) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept() if self._listener else (None, None)
            except socket.timeout:
                continue
            except OSError:
                break
            if client is None:
                continue
            with self._clients_lock:
                self._clients.add(client)
            threading.Thread(
                target=self._handle_client,
                args=(client, first, second),
                name="proxy-chain-connection",
                daemon=True,
            ).start()

    def _handle_client(self, client: socket.socket, first, second) -> None:
        upstream = None
        try:
            client.settimeout(20)
            request = _read_headers(client)
            line = request.split(b"\r\n", 1)[0].decode("iso-8859-1", "replace")
            parts = line.split()
            if len(parts) != 3 or parts[0].upper() != "CONNECT":
                raise ProxyChainError("代理链只支持 CONNECT 请求")
            target_host, target_port_text = parts[1].rsplit(":", 1)
            target_port = int(target_port_text)
            _, first_host, first_port, _, _ = first
            upstream = socket.create_connection((first_host, first_port), timeout=20)
            if first[0] == "https":
                context = ssl.create_default_context()
                upstream = context.wrap_socket(upstream, server_hostname=first_host)
            _connect_via_proxy(upstream, first, second[1], second[2])
            _connect_via_proxy(upstream, second, target_host.strip("[]"), target_port)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: grok-register\r\n\r\n")
            client.settimeout(None)
            upstream.settimeout(None)
            self._relay(client, upstream)
        except Exception as exc:
            try:
                message = str(exc).replace("\r", " ").replace("\n", " ")[:180]
                client.sendall(
                    f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(message)}\r\n"
                    "Connection: close\r\n\r\n" + message
                .encode("utf-8", "replace"))
            except Exception:
                pass
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            with self._clients_lock:
                self._clients.discard(client)

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket) -> None:
        while True:
            readable, _, _ = select.select([left, right], [], [], 60)
            if not readable:
                continue
            for source in readable:
                data = source.recv(64 * 1024)
                if not data:
                    return
                (right if source is left else left).sendall(data)

    def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            self._listener = None
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None
        self._url = ""
