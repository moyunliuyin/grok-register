import unittest
from unittest import mock

from backend.registration import workspace as workspace_module
from backend.registration.workspace import extract_team_id, setup_workspace


TEAM_ID = "123e4567-e89b-12d3-a456-426614174000"


class WorkspacePage:
    def __init__(self, *, team_url="", advance=False):
        self.url = team_url or "https://console.x.ai/"
        self.advance = advance
        self.get_calls = []
        self.cookie_calls = []
        self.run_js_calls = []
        self.wait = mock.Mock()

        class CookieSetter:
            def __init__(inner, owner):
                inner.owner = owner

            def __call__(inner, cookies):
                inner.owner.cookie_calls.append(cookies)

        self.set = mock.Mock()
        self.set.cookies = CookieSetter(self)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        self.url = url

    def run_js(self, script, *args):
        self.run_js_calls.append((script, args))
        if "readyState" in script:
            return {"url": self.url, "readyState": "complete"}
        if "explore" in script.lower():
            return {"url": self.url, "found": True, "clicked": True}
        if "welcome-team-name" in script:
            return {"url": self.url, "welcome_present": True, "filled": True, "value": args[0] if args else ""}
        if self.advance:
            self.url = f"https://console.x.ai/team/{TEAM_ID}"
            return {"url": self.url, "continue_clicked": True}
        return {"url": self.url, "continue_clicked": False}


class WorkspaceSetupTests(unittest.TestCase):
    def test_extract_team_id_from_team_path_and_created_query(self):
        self.assertEqual(
            extract_team_id(f"https://console.x.ai/team/{TEAM_ID}/overview"),
            TEAM_ID,
        )
        self.assertEqual(
            extract_team_id(f"https://console.x.ai/?created={TEAM_ID}"),
            TEAM_ID,
        )
        self.assertEqual(extract_team_id("https://console.x.ai/team/not-a-uuid"), "")

    def test_existing_team_url_is_reported_without_navigation(self):
        page = WorkspacePage(team_url=f"https://console.x.ai/team/{TEAM_ID}")

        result = setup_workspace(page, "Fixture Team")

        self.assertEqual(result["status"], "already_configured")
        self.assertEqual(result["team_id"], TEAM_ID)
        self.assertEqual(page.get_calls, [])

    def test_welcome_flow_navigates_and_returns_created_team(self):
        page = WorkspacePage(advance=True)

        with mock.patch.object(
            workspace_module,
            "_select_explore_and_pause",
            return_value=True,
        ):
            result = setup_workspace(
                page,
                "Fixture Team",
                sso_token="secret-sso",
                timeout=1,
            )

        self.assertEqual(result["status"], "created")
        self.assertEqual(result["team_id"], TEAM_ID)
        self.assertEqual(result["workspace_name"], "Fixture Team")
        self.assertEqual(len(page.cookie_calls), 1)
        self.assertIn(("Fixture Team",), [args for _, args in page.run_js_calls])

    def test_missing_team_id_returns_failure_without_secret(self):
        page = WorkspacePage()

        result = setup_workspace(
            page,
            "Fixture Team",
            sso_token="secret-sso",
            timeout=0.1,
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["team_id"], "")
        self.assertNotIn("secret-sso", result["error"])

    def test_api_key_timeout_does_not_fallback_to_skip_for_now(self):
        page = WorkspacePage()
        logs = []

        with mock.patch.object(
            workspace_module.time,
            "monotonic",
            side_effect=[0.0, 31.0],
        ), mock.patch.object(
            workspace_module,
            "_click_skip_for_now",
        ) as click_skip:
            result = workspace_module._finish_api_key_screen(page, logs.append)

        self.assertFalse(result)
        click_skip.assert_not_called()
        self.assertIn("Get started", logs[-1])

    def test_dashboard_cannot_bypass_unfinished_api_key_flow(self):
        page = WorkspacePage()

        with mock.patch.object(
            workspace_module,
            "_select_explore_starting_point",
            return_value="skip",
        ), mock.patch.object(
            workspace_module,
            "_finish_api_key_screen",
            return_value=False,
        ), mock.patch.object(
            workspace_module,
            "_console_dashboard_ready",
            return_value=True,
        ) as dashboard_ready:
            with self.assertRaises(workspace_module.WorkspaceSetupError):
                workspace_module._select_explore_and_pause(
                    page,
                    deadline=100.0,
                )

        dashboard_ready.assert_not_called()

    def test_direct_console_dashboard_allows_cpa_after_create_api_key(self):
        page = WorkspacePage()
        logs = []

        with mock.patch.object(
            workspace_module,
            "_console_dashboard_ready",
            return_value=True,
        ), mock.patch.object(workspace_module.time, "sleep"):
            result = workspace_module._finish_api_key_screen(page, logs.append)

        self.assertTrue(result)
        self.assertIn("Create API key", logs[-1])

    def test_explore_route_can_click_direct_skip_for_now(self):
        page = WorkspacePage()

        with mock.patch.object(
            workspace_module,
            "_console_dashboard_ready",
            side_effect=[False, True],
        ), mock.patch.object(
            workspace_module,
            "_click_skip_for_now",
            return_value=True,
        ) as click_skip, mock.patch.object(workspace_module.time, "sleep"):
            result = workspace_module._finish_api_key_screen(
                page,
                allow_direct_skip=True,
            )

        self.assertTrue(result)
        click_skip.assert_called_once()


if __name__ == "__main__":
    unittest.main()
