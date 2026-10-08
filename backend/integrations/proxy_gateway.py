"""Client for the Skyline proxy-gateway upstream switcher."""

from __future__ import annotations

from urllib.parse import urlsplit

import requests


class ProxyGatewayError(RuntimeError):
    pass


class ProxyGatewayClient:
    def __init__(self, panel_url: str, panel_password: str, proxy_url: str):
        self.panel_url = str(panel_url or "").strip().rstrip("/")
        self.panel_password = str(panel_password or "").strip()
        self.proxy_url = str(proxy_url or "").strip()
        parsed = urlsplit(self.panel_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProxyGatewayError("代理网关面板地址无效")
        if not self.panel_password:
            raise ProxyGatewayError("代理网关面板密码为空")
        proxy_parsed = urlsplit(self.proxy_url)
        if proxy_parsed.scheme not in {"http", "https"} or not proxy_parsed.hostname:
            raise ProxyGatewayError("代理网关入口地址无效")

    def replace_upstream(self, link: str) -> str:
        """Validate and replace one upstream, returning the reported exit IP."""
        value = str(link or "").strip()
        if not value:
            raise ProxyGatewayError("代理池节点为空")
        with requests.Session() as session:
            try:
                login = session.post(
                    f"{self.panel_url}/api/login",
                    json={"password": self.panel_password},
                    timeout=20,
                )
                login_data = login.json()
            except Exception as exc:
                raise ProxyGatewayError(f"代理网关登录失败: {exc}") from exc
            if login.status_code >= 400 or not login_data.get("ok"):
                raise ProxyGatewayError(
                    f"代理网关登录失败 HTTP {login.status_code}"
                )
            try:
                result = session.post(
                    f"{self.panel_url}/api/set",
                    json={"link": value},
                    timeout=30,
                )
                data = result.json()
            except Exception as exc:
                raise ProxyGatewayError(f"代理网关切换失败: {exc}") from exc
            if result.status_code >= 400 or not data.get("ok"):
                detail = str(data.get("error") or f"HTTP {result.status_code}")
                raise ProxyGatewayError(f"代理网关切换失败: {detail}")
            return str(data.get("exit_ip") or "").strip()
