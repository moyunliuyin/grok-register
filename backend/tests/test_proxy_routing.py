import base64
from collections import Counter
import socketserver
import threading
import unittest
from unittest import mock

from backend.automation import session as browser_session
from backend.integrations import auth_exchange
from backend.integrations import network_checks
from backend.registration import engine as gr


class ProxyRoutingTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(gr.config)
        gr.config["proxy_gateway_enabled"] = False

    def tearDown(self):
        gr.config.clear()
        gr.config.update(self.original_config)
        browser_session.configure(
            get_proxies=lambda: {},
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )

    def test_camoufox_registration_keeps_configured_proxy(self):
        browser_session.configure(
            get_proxies=lambda: {
                "http": "http://127.0.0.1:7897",
                "https": "http://127.0.0.1:7897",
            },
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(options["proxy"], {"server": "http://127.0.0.1:7897"})

    def test_proxy_pool_rotates_thread_safely(self):
        pool = gr.ProxyPool(("http://proxy-a.example.com:8080", "http://proxy-b.example.com:8080"))
        values = []
        values_lock = threading.Lock()

        def acquire():
            value, _, _ = pool.acquire()
            with values_lock:
                values.append(value)

        threads = [threading.Thread(target=acquire) for _ in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(values), 32)
        self.assertEqual(Counter(values), Counter({
            "http://proxy-a.example.com:8080": 16,
            "http://proxy-b.example.com:8080": 16,
        }))

    def test_advance_proxy_pool_moves_preflight_peek(self):
        gr.config["proxy_pool"] = (
            "proxy-a.example.com:8080:user:pass\n"
            "proxy-b.example.com:8080:user:pass"
        )
        first = gr.current_account_proxy()
        self.assertTrue(gr.advance_proxy_pool())
        second = gr.current_account_proxy()
        self.assertNotEqual(first, second)

    def test_account_proxy_is_sticky_and_rotates_after_release(self):
        gr.config["proxy_pool"] = (
            "proxy-a.example.com:8080:user:pass\n"
            "proxy-b.example.com:8080:user:pass"
        )
        logs = []
        first = gr.begin_account_proxy(log_callback=logs.append)
        self.assertEqual(gr.get_proxies(), {"http": first, "https": first})
        self.assertEqual(gr.current_account_proxy(), first)
        gr.end_account_proxy()
        second = gr.begin_account_proxy(log_callback=logs.append)
        self.assertNotEqual(first, second)
        self.assertEqual(gr.get_proxies(), {"http": second, "https": second})
        self.assertNotIn("user:pass", "\n".join(logs))
        gr.end_account_proxy()

    def test_browser_reads_current_account_proxy_from_pool(self):
        gr.config["proxy_pool"] = "proxy-a.example.com:8080:user:pass"
        gr.config["proxy_chain_via_local"] = False
        selected = gr.begin_account_proxy()
        try:
            browser_session.configure(
                get_proxies=gr.get_proxies,
                is_debug=lambda: False,
                is_headless=lambda: False,
                get_locale=lambda: "en-US",
            )
            options = browser_session.create_browser_options(unique_profile=False)
        finally:
            gr.end_account_proxy()
        self.assertEqual(
            options["proxy"],
            {
                "server": "http://proxy-a.example.com:8080",
                "username": "user",
                "password": "pass",
            },
        )

    def test_camoufox_registration_uses_authenticated_http_proxy(self):
        browser_session.configure(
            get_proxies=lambda: {
                "http": "http://proxy-user:proxy-password@proxy.example.com:7897",
                "https": "http://proxy-user:proxy-password@proxy.example.com:7897",
            },
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(
            options["proxy"],
            {
                "server": "http://proxy.example.com:7897",
                "username": "proxy-user",
                "password": "proxy-password",
            },
        )

    def test_camoufox_decodes_percent_encoded_http_credentials(self):
        browser_session.configure(
            get_proxies=lambda: {
                "https": "http://user%40mail:p%40ss%3Aword@proxy.example.com:7897"
            },
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(
            options["proxy"],
            {
                "server": "http://proxy.example.com:7897",
                "username": "user@mail",
                "password": "p@ss:word",
            },
        )

    def test_cloakbrowser_reuses_authenticated_proxy_parsing(self):
        browser_session.configure(
            get_proxies=lambda: {
                "https": "http://user%40mail:p%40ss%3Aword@proxy.example.com:7897"
            },
            is_debug=lambda: False,
            is_headless=lambda: False,
            get_locale=lambda: "en-US",
            get_engine=lambda: "cloakbrowser",
        )
        options = browser_session.create_browser_options(unique_profile=False)
        self.assertEqual(
            options["proxy"],
            {
                "server": "http://proxy.example.com:7897",
                "username": "user@mail",
                "password": "p@ss:word",
            },
        )

    def test_http_client_sends_encoded_proxy_credentials_as_basic_auth(self):
        captured = {}

        class ProxyHandler(socketserver.StreamRequestHandler):
            def handle(self):
                lines = []
                while True:
                    line = self.rfile.readline().decode("iso-8859-1").rstrip("\r\n")
                    if not line:
                        break
                    lines.append(line)
                for line in lines[1:]:
                    if line.lower().startswith("proxy-authorization:"):
                        captured["authorization"] = line.split(":", 1)[1].strip()
                self.wfile.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Length: 2\r\n"
                    b"Connection: close\r\n\r\nOK"
                )

        with socketserver.TCPServer(("127.0.0.1", 0), ProxyHandler) as proxy_server:
            thread = threading.Thread(target=proxy_server.handle_request, daemon=True)
            thread.start()
            port = proxy_server.server_address[1]
            proxy = f"http://user%40mail:p%40ss%3Aword@127.0.0.1:{port}"
            with mock.patch.object(gr, "registration_log"):
                response = gr.http_get(
                    "http://registration.test/probe",
                    proxies={"http": proxy},
                    timeout=5,
                )
            thread.join(timeout=5)

        expected = "Basic " + base64.b64encode(b"user@mail:p@ss:word").decode("ascii")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured.get("authorization"), expected)

    def test_actual_http_route_log_deduplicates_query_variants(self):
        logs = []
        with mock.patch.object(gr, "registration_log", side_effect=logs.append):
            gr.reset_network_route_logs()
            gr._log_actual_http_route(
                "get",
                "https://accounts.x.ai/sign-up?step=1",
                proxies={"https": "http://127.0.0.1:7897"},
            )
            gr._log_actual_http_route(
                "GET",
                "https://accounts.x.ai/sign-up?step=2",
                proxies={"https": "http://127.0.0.1:7897"},
            )
            gr._log_actual_http_route("GET", "http://mail.test/api/emails", proxies={})

        self.assertEqual(len(logs), 2)
        self.assertIn("GET https://accounts.x.ai/sign-up -> 代理 http://127.0.0.1:7897", logs[0])
        self.assertIn("GET http://mail.test/api/emails -> 直连（不使用代理）", logs[1])

    def test_actual_http_route_log_redacts_proxy_credentials(self):
        logs = []
        proxy = "http://proxy-user:p%40ss@proxy.example.com:7897"
        with mock.patch.object(gr, "registration_log", side_effect=logs.append):
            gr.reset_network_route_logs()
            gr._log_actual_http_route(
                "GET",
                "https://accounts.x.ai/sign-up",
                proxies={"https": proxy},
            )

        self.assertEqual(len(logs), 1)
        self.assertNotIn("proxy-user", logs[0])
        self.assertNotIn("p%40ss", logs[0])
        self.assertIn("代理 http://***:***@proxy.example.com:7897", logs[0])

    def test_outlook_acquire_and_code_polling_use_direct_default_http(self):
        with mock.patch.object(
            gr.outlookemail_provider,
            "acquire_email",
            return_value=("fixture@outlook.com", "fixture-token"),
        ) as acquire:
            gr.outlookemail_get_email_and_token()
        self.assertIs(acquire.call_args.args[0], gr.http_get)
        self.assertIs(acquire.call_args.args[1], gr.direct_http_session)
        self.assertEqual(acquire.call_args.kwargs["proxies"], {})

        with mock.patch.object(
            gr.outlookemail_provider,
            "wait_for_code",
            return_value="ABC-123",
        ) as wait:
            gr.outlookemail_get_oai_code("fixture@outlook.com")
        self.assertIs(wait.call_args.args[0], gr.http_get)
        self.assertIs(wait.call_args.args[1], gr.direct_http_session)
        self.assertEqual(wait.call_args.kwargs["proxies"], {})

    def test_default_http_wrappers_disable_environment_and_project_proxy(self):
        gr.config["proxy"] = "http://127.0.0.1:7897"
        for method, request_fn in (
            ("GET", gr.http_get),
            ("POST", gr.http_post),
            ("DELETE", gr.http_delete),
        ):
            with self.subTest(method=method):
                response = mock.Mock()
                session = mock.MagicMock()
                session.__enter__.return_value = session
                session.__exit__.return_value = False
                session.request.return_value = response
                raw_request = session.request
                with mock.patch.object(
                    gr.requests, "Session", return_value=session
                ) as factory:
                    result = request_fn("http://mail-service.test/api")
                self.assertIs(result, response)
                factory.assert_called_once_with(trust_env=False)
                raw_request.assert_called_once_with(
                    method,
                    "http://mail-service.test/api",
                    proxies={},
                    timeout=15,
                )

    def test_mailnest_uses_standard_requests_client(self):
        response = mock.Mock(status_code=200)
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        session.request.return_value = response
        with mock.patch.object(
            gr._stdlib_requests, "Session", return_value=session
        ) as factory, mock.patch.object(
            gr, "get_proxies", return_value={
                "http": "http://127.0.0.1:7890",
                "https": "http://127.0.0.1:7890",
            }
        ):
            result = gr.http_get(
                "https://mailnest.top/",
                headers={"Accept": "application/json"},
                timeout=12,
            )
        self.assertIs(result, response)
        factory.assert_called_once_with()
        self.assertTrue(session.trust_env)
        session.request.assert_called_once_with(
            "GET",
            "https://mailnest.top/",
            headers={"Accept": "application/json"},
            proxies={
                "http": "http://127.0.0.1:7890",
                "https": "http://127.0.0.1:7890",
            },
            timeout=12,
        )

    def test_xai_connectivity_check_explicitly_uses_configured_proxy(self):
        response = mock.Mock(status_code=200, text="<!doctype html>", headers={})
        http_get = mock.Mock(return_value=response)
        proxy = "http://127.0.0.1:7897"
        _, ok, detail = network_checks.check_xai_signup(proxy, http_get)
        self.assertTrue(ok, detail)
        self.assertEqual(
            http_get.call_args.kwargs["proxies"],
            {"http": proxy, "https": proxy},
        )

    def test_outlook_connectivity_check_uses_direct_default_http(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"success": True, "accounts": []}
        direct_get = mock.Mock(return_value=response)
        name, ok, detail = network_checks.check_email_api(
            "outlookemail",
            {
                "outlookemail_api_base": "http://mail-pool.test",
                "outlookemail_source": "accounts",
                "outlookemail_api_key": "api-key",
                "outlookemail_group_id": "",
            },
            direct_get,
            mock.Mock(),
        )
        self.assertEqual(name, "邮箱API")
        self.assertTrue(ok, detail)
        self.assertEqual(direct_get.call_args.kwargs["proxies"], {})

    def test_outlook_disable_is_forced_direct(self):
        gr.config.update(
            {
                "email_provider": "outlookemail",
                "outlookemail_source": "accounts",
                "outlookemail_disable_after_cpa_success": True,
            }
        )
        with mock.patch.object(
            gr.outlookemail_provider,
            "account_for_email",
            return_value={"id": 1, "email": "fixture@outlook.com"},
        ) as lookup, mock.patch.object(
            gr.outlookemail_provider,
            "disable_account",
            return_value={"success": True, "account_id": 1},
        ) as disable:
            detail = gr.disable_outlookemail_after_cpa_success(
                "fixture@outlook.com", {"status": "success"}
            )
        self.assertEqual(detail["status"], "success")
        self.assertIs(lookup.call_args.args[0], gr.http_get)
        self.assertIs(disable.call_args.args[0], gr.http_get)
        self.assertIs(disable.call_args.args[1], gr.direct_http_session)
        self.assertEqual(disable.call_args.kwargs["proxies"], {})

    def test_sso_token_exchange_uses_proxy_but_cpa_remote_upload_is_direct(self):
        proxy = "http://proxy-user:p%40ss@127.0.0.1:7897"
        gr.config.update(
            {
                "proxy": proxy,
                "cpa_auto_add": True,
                "proxy_pool": "",
                "cpa_token_mode": "device_protocol",
                "cpa_auth_dir": "",
                "cpa_remote_url": "http://cpa.internal:8317",
                "cpa_management_key": "management-key",
                "grok2api_auth_dir": "",
                "grok2api_remote_url": "",
                "grok2api_remote_username": "",
                "grok2api_remote_password": "",
            }
        )
        with mock.patch.object(
            gr._s2cpa,
            "sso_to_token",
            return_value={"access_token": "access", "refresh_token": "refresh"},
        ) as exchange, mock.patch.object(
            gr._s2cpa,
            "token_to_cpa_record",
            return_value={"access_token": "access", "email": "fixture@example.com"},
        ), mock.patch.object(
            gr._s2cpa,
            "decode_jwt_payload",
            return_value={},
        ), mock.patch.object(
            gr._s2cpa,
            "upload_cpa_auth_remote",
            return_value="xai-fixture.json",
        ) as upload:
            logs = []
            self.assertTrue(
                gr.add_sso_to_cpa(
                    "sso-value",
                    email="fixture@example.com",
                    log_callback=logs.append,
                )
            )

        self.assertEqual(exchange.call_args.kwargs["proxy"], proxy)
        self.assertEqual(upload.call_args.kwargs["proxy"], "")
        rendered_logs = "\n".join(logs)
        self.assertNotIn("proxy-user", rendered_logs)
        self.assertNotIn("p%40ss", rendered_logs)
        self.assertIn("proxy=http://***:***@127.0.0.1:7897", rendered_logs)

    def test_cpa_remote_http_session_does_not_inherit_environment_proxy(self):
        response = mock.Mock(status_code=200, reason="OK", text="")
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = False
        session.post.return_value = response
        with mock.patch.object(auth_exchange._stdlib_requests, "Session", return_value=session) as factory:
            name = auth_exchange.upload_cpa_auth_remote(
                "http://cpa.internal:8317",
                "management-key",
                {"email": "fixture@example.com"},
                proxy="",
            )
        self.assertEqual(name, "xai-fixture@example.com.json")
        factory.assert_called_once_with()
        self.assertIsNone(session.post.call_args.kwargs["proxies"])


if __name__ == "__main__":
    unittest.main()
