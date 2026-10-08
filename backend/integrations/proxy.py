"""代理地址解析、校验和脱敏。

HTTP 客户端继续使用配置中的完整代理 URL；Camoufox 需要把认证信息拆成
``server``、``username`` 和 ``password``。容器运行时还会把回环地址映射为
Docker Host 别名，同时保留原始认证信息。
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit


LOCAL_PROXY_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
HTTP_PROXY_SCHEMES = frozenset({"http", "https"})

_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_AUTHENTICATED_PROXY_IN_TEXT = re.compile(
    r"([A-Za-z][A-Za-z0-9+.-]*://)([^\s]+)@"
)


def normalize_proxy_entry(proxy: str) -> str:
    """Normalize one proxy-pool entry to a validated HTTP(S) proxy URL.

    In addition to regular HTTP(S) URLs, accept ``host:port:user:password``
    and encode the credentials before handing the value to HTTP clients.
    """
    value = str(proxy or "").strip()
    if not value:
        return ""
    if "://" in value:
        return validate_http_proxy_url(value)

    parts = value.split(":", 3)
    if len(parts) != 4 or not all(parts):
        raise ValueError("裸代理必须使用 host:port:user:password 格式")
    host, port, username, password = parts
    if any(char.isspace() for char in (host, port, username, password)):
        raise ValueError("代理地址不能包含空白字符")
    if ":" in host:
        raise ValueError("裸代理主机暂不支持未加括号的 IPv6 地址")
    try:
        port_number = int(port)
    except ValueError as exc:
        raise ValueError("代理端口必须是数字") from exc
    if not 1 <= port_number <= 65535:
        raise ValueError("代理端口必须在 1 到 65535 之间")
    normalized = (
        f"http://{quote(username, safe='')}:{quote(password, safe='')}@"
        f"{host}:{port_number}"
    )
    return validate_http_proxy_url(normalized)


def normalize_proxy_pool(value: object) -> str:
    """Normalize a multiline/whitespace-separated proxy pool.

    The returned value is safe to persist as one normalized URL per line.
    Empty input is accepted and returns an empty string.
    """
    entries = []
    for index, raw in enumerate(re.split(r"\s+", str(value or "").strip()), start=1):
        if not raw:
            continue
        try:
            entries.append(normalize_proxy_entry(raw))
        except ValueError as exc:
            raise ValueError(f"第 {index} 条代理格式错误: {exc}") from exc
    return "\n".join(entries)


def _proxy_host_port(parsed) -> str:
    """Return a URL netloc containing only host and optional port."""
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port
    return f"{host}:{port}" if port is not None else host


def _decode_credential(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if _BAD_PERCENT_ESCAPE.search(value):
        raise ValueError(f"{label}包含无效的百分号编码")
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label}不是有效的 UTF-8 百分号编码") from exc
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded):
        raise ValueError(f"{label}不能包含控制字符")
    return decoded


def validate_http_proxy_url(proxy_url: str) -> str:
    """Validate an HTTP(S) proxy URL and return its trimmed original value.

    The original, still-percent-encoded URL is deliberately returned so HTTP
    libraries can parse authentication themselves. Decoding is only performed
    when building Camoufox/Playwright options.
    """
    value = str(proxy_url or "").strip()
    if not value:
        return ""
    if any(char.isspace() for char in value):
        raise ValueError("代理地址不能包含空白字符")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
    except ValueError as exc:
        raise ValueError(f"代理地址无效: {exc}") from exc
    if scheme not in HTTP_PROXY_SCHEMES:
        raise ValueError("HTTP 认证代理需使用 http:// 或 https://")
    if not host:
        raise ValueError("代理地址缺少主机名")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段；凭据特殊字符请使用百分号编码")
    try:
        parsed.port  # Accessing this property validates the port syntax/range.
    except ValueError as exc:
        raise ValueError(f"代理地址无效: {exc}") from exc
    if "@" in parsed.netloc and parsed.username in (None, ""):
        raise ValueError("代理认证用户名不能为空")
    _decode_credential(parsed.username, "代理用户名")
    _decode_credential(parsed.password, "代理密码")
    return value


def parse_http_proxy_url(proxy_url: str) -> dict[str, str]:
    """Build separated HTTP(S) proxy fields for Camoufox/Playwright."""
    value = validate_http_proxy_url(proxy_url)
    if not value:
        return {}
    parsed = urlsplit(value)
    result = {
        "server": urlunsplit(
            (parsed.scheme.lower(), _proxy_host_port(parsed), "", "", "")
        )
    }
    username = _decode_credential(parsed.username, "代理用户名")
    password = _decode_credential(parsed.password, "代理密码")
    if username is not None:
        result["username"] = username
    if password is not None:
        result["password"] = password
    return result


def redact_proxy_url(proxy_url: str) -> str:
    """Hide proxy userinfo while retaining a useful scheme/host/port display."""
    value = str(proxy_url or "").strip()
    if not value or "@" not in value:
        return value
    has_scheme = "://" in value
    try:
        parsed = urlsplit(value if has_scheme else f"http://{value}")
        if "@" not in parsed.netloc or not parsed.hostname:
            raise ValueError("missing proxy host")
        redacted = urlunsplit(
            (
                parsed.scheme,
                f"***:***@{_proxy_host_port(parsed)}",
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
        return redacted if has_scheme else redacted.split("://", 1)[1]
    except ValueError:
        return _AUTHENTICATED_PROXY_IN_TEXT.sub(r"\1***:***@", value)


def redact_proxy_text(value: object) -> str:
    """Redact authenticated proxy URLs embedded in log or exception text."""
    return _AUTHENTICATED_PROXY_IN_TEXT.sub(
        r"\1***:***@", str(value if value is not None else "")
    )


def resolve_proxy_url(proxy_url: str) -> str:
    """Replace a local proxy host with the Docker host alias when configured."""
    value = str(proxy_url or "").strip()
    docker_host = str(os.environ.get("GROK_DOCKER_PROXY_HOST", "") or "").strip()
    if not value or not docker_host:
        return value

    has_scheme = "://" in value
    try:
        parsed = urlsplit(value if has_scheme else f"http://{value}")
        parsed.port
    except ValueError:
        return value
    if (parsed.hostname or "").lower() not in LOCAL_PROXY_HOSTS:
        return value

    auth = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
    port = f":{parsed.port}" if parsed.port is not None else ""
    resolved = urlunsplit(
        (parsed.scheme, f"{auth}{docker_host}{port}", parsed.path, parsed.query, parsed.fragment)
    )
    return resolved if has_scheme else resolved.split("://", 1)[1]
