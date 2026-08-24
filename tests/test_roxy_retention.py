# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import roxy_flow


class FakeDriver:
    def __init__(self, events):
        self.events = events

    def set_page_load_timeout(self, _value):
        pass

    def set_script_timeout(self, _value):
        pass

    def quit(self):
        self.events.append("driver.quit")


class FakeClient:
    def __init__(self, events, profile_proxy=None):
        self.events = events
        self.profile_proxy = profile_proxy

    def open_profile(self, **kwargs):
        self.events.append(f"open:{kwargs.get('require_proxy_exit_ip')}")
        return SimpleNamespace(
            profile_id="profile-1", preflight_exit_geo={"ip": "203.0.113.10"},
            debugger_address="127.0.0.1:9333", ws_endpoint=None,
        )

    def cleanup_profile(self, _opened):
        self.events.append("cleanup")

    def close_profile(self, profile_id):
        self.events.append(f"close:{profile_id}")
        return True

    def delete_profile(self, profile_id):
        self.events.append(f"delete:{profile_id}")
        return True


class RoxyRetentionTests(unittest.TestCase):
    def tearDown(self):
        with roxy_flow._RETAINED_LOCK:
            roxy_flow._RETAINED.clear()

    def _loaded(self, events, fetch_session=None):
        client_box = {}

        def client_factory(profile_proxy=None):
            client = FakeClient(events, profile_proxy)
            client_box["client"] = client
            return client

        driver = FakeDriver(events)
        loaded = (
            client_factory,
            lambda _opened: driver,
            lambda _driver: None,
            fetch_session or (lambda *_a, **_k: {}),
            lambda *_a, **_k: None,
            lambda *_a, **_k: None,
            lambda *_a, **_k: {"ip": "203.0.113.10", "country": "US"},
        )
        return loaded, driver, client_box

    def test_success_keeps_logged_in_window_and_close_button_deletes_profile(self):
        events = []
        loaded, _driver, _client_box = self._loaded(events)
        with patch.object(roxy_flow, "_load_main_roxy", return_value=loaded), \
                patch.object(roxy_flow, "_complete_login", side_effect=[
                    {"user": {"email": "old@example.com"}},
                    {"user": {"email": "new@example.com"}, "accessToken": "at-new"},
                ]), \
                patch.object(roxy_flow, "_change_email_har_guided"), \
                patch.object(roxy_flow, "_clear_login_state"):
            result = roxy_flow.perform_email_rebind(
                old_email="old@example.com", new_email="new@example.com",
                password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                api_url="https://mail.example/code", proxy_url="http://proxy.example:8000",
            )

        self.assertEqual(result["roxy_browser_status"], "open")
        self.assertEqual(result["roxy_profile_id"], "profile-1")
        self.assertEqual(result["roxy_cdp_port"], 9333)
        self.assertNotIn("driver.quit", events)
        self.assertNotIn("cleanup", events)
        self.assertIn("profile-1", roxy_flow._RETAINED)

        self.assertTrue(roxy_flow.delete_retained_profile("profile-1"))
        self.assertIn("close:profile-1", events)
        self.assertIn("delete:profile-1", events)
        self.assertIn("driver.quit", events)
        self.assertNotIn("profile-1", roxy_flow._RETAINED)

    def test_api_failure_quits_driver_and_deletes_temporary_profile_before_retry(self):
        events = []
        loaded, _driver, _client_box = self._loaded(events)
        with patch.object(roxy_flow, "_load_main_roxy", return_value=loaded), \
                patch.object(roxy_flow, "_complete_login", return_value={"user": {"email": "old@example.com"}}), \
                patch.object(roxy_flow, "_change_email_har_guided", side_effect=roxy_flow._HarApiUnavailable("API unavailable")):
            with self.assertRaisesRegex(roxy_flow._HarApiUnavailable, "API unavailable"):
                roxy_flow.perform_email_rebind(
                    old_email="old@example.com", new_email="new@example.com",
                    password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                    api_url="https://mail.example/code", proxy_url="http://proxy.example:8000",
                )

        self.assertIn("driver.quit", events)
        self.assertIn("close:profile-1", events)
        self.assertIn("delete:profile-1", events)
        self.assertLess(events.index("driver.quit"), events.index("close:profile-1"))
        self.assertLess(events.index("close:profile-1"), events.index("delete:profile-1"))
        self.assertFalse(roxy_flow._RETAINED)

    def test_replacement_login_retries_with_new_email_until_access_token_is_obtained(self):
        events = []
        progress_events = []
        loaded, _driver, _client_box = self._loaded(events)
        with patch.object(roxy_flow, "_load_main_roxy", return_value=loaded), \
                patch.object(roxy_flow, "_complete_login", side_effect=[
                    {"user": {"email": "old@example.com"}},
                    TimeoutError("替换邮箱验证码已提交但未取得 ChatGPT accessToken"),
                    {"user": {"email": "new@example.com"}, "accessToken": "at-new"},
                ]) as complete_login, \
                patch.object(roxy_flow.mail_api, "read_current_otp", return_value=None), \
                patch.object(roxy_flow, "_change_email_har_guided"), \
                patch.object(roxy_flow, "_clear_login_state"), \
                patch.object(roxy_flow.settings, "TRANSIENT_RETRY_DELAY", 0):
            result = roxy_flow.perform_email_rebind(
                old_email="old@example.com", new_email="new@example.com",
                password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                api_url="https://mail.example/code", proxy_url="http://proxy.example:8000",
                max_relogin_retries=1,
                progress=lambda stage, _message: progress_events.append(stage),
            )

        self.assertEqual(result["access_token"], "at-new")
        self.assertEqual(complete_login.call_count, 3)
        self.assertIn("relogin_new_retry", progress_events)
        self.assertIn("profile-1", roxy_flow._RETAINED)

    def test_direct_replacement_login_keeps_new_email_window(self):
        events = []
        loaded, _driver, _client_box = self._loaded(events)
        with patch.object(roxy_flow, "_load_main_roxy", return_value=loaded), \
                patch.object(roxy_flow, "_complete_login", return_value={
                    "user": {"email": "new@example.com"}, "accessToken": "at-new",
                }), \
                patch.object(roxy_flow.mail_api, "read_current_otp", return_value=None), \
                patch.object(roxy_flow, "_clear_login_state"):
            result = roxy_flow.perform_replacement_login(
                new_email="new@example.com", password="", totp_secret="",
                api_url="https://mail.example/new", proxy_url="http://proxy.example:8000",
                max_relogin_retries=0,
            )

        self.assertEqual(result["access_token"], "at-new")
        self.assertEqual(result["email"], "new@example.com")
        self.assertEqual(result["roxy_browser_status"], "open")
        self.assertEqual(result["roxy_cdp_port"], 9333)
        self.assertNotIn("driver.quit", events)

    def test_close_button_deletes_profile_after_service_restart(self):
        events = []
        loaded, _driver, _client_box = self._loaded(events)
        with patch.object(roxy_flow, "_load_main_roxy", return_value=loaded):
            self.assertTrue(roxy_flow.delete_retained_profile("profile-1"))

        self.assertEqual(events, ["close:profile-1", "delete:profile-1"])
        self.assertFalse(roxy_flow._RETAINED)

    def test_refresh_reuses_retained_driver_and_validates_new_email(self):
        events = []
        updated_session = {"user": {"email": "new@example.com"}, "accessToken": "at-plus"}
        loaded, driver, _client_box = self._loaded(events, fetch_session=lambda *_a, **_k: updated_session)
        client = FakeClient(events)
        roxy_flow._retain_browser(
            profile_id="profile-1", email="new@example.com", client=client,
            opened=SimpleNamespace(
                profile_id="profile-1", debugger_address="127.0.0.1:9333",
                ws_endpoint=None,
            ), driver=driver,
        )
        with patch.object(roxy_flow, "_load_main_roxy", return_value=loaded):
            result = roxy_flow.refresh_retained_access_token("profile-1", "new@example.com")

        self.assertEqual(result["access_token"], "at-plus")
        self.assertEqual(result["email"], "new@example.com")
        self.assertEqual(result["roxy_cdp_port"], 9333)
        self.assertNotIn("driver.quit", events)

    def test_refresh_reopens_same_profile_after_service_restart(self):
        events = []
        session = {"user": {"email": "new@example.com"}, "accessToken": "at-reopened"}
        loaded, _driver, _client_box = self._loaded(events, fetch_session=lambda *_a, **_k: session)
        reopened = SimpleNamespace(profile_id="profile-1")
        with patch.object(roxy_flow, "_load_main_roxy", return_value=loaded), \
                patch.object(roxy_flow, "_open_existing_profile", return_value=reopened) as open_existing:
            result = roxy_flow.refresh_retained_access_token("profile-1", "new@example.com")

        open_existing.assert_called_once()
        self.assertEqual(open_existing.call_args.args[1], "profile-1")
        self.assertEqual(result["access_token"], "at-reopened")
        self.assertIn("profile-1", roxy_flow._RETAINED)

    def test_cdp_port_supports_debugger_and_websocket_addresses(self):
        self.assertEqual(roxy_flow._roxy_cdp_port(SimpleNamespace(
            debugger_address="127.0.0.1:9222", ws_endpoint=None,
        )), 9222)
        self.assertEqual(roxy_flow._roxy_cdp_port(SimpleNamespace(
            debugger_address=None,
            ws_endpoint="ws://127.0.0.1:9444/devtools/browser/example",
        )), 9444)
        self.assertIsNone(roxy_flow._roxy_cdp_port(SimpleNamespace(
            debugger_address="bad-address", ws_endpoint=None,
        )))

    def test_resolve_cdp_port_prefers_retained_profile(self):
        events = []
        roxy_flow._retain_browser(
            profile_id="profile-1", email="new@example.com",
            client=FakeClient(events),
            opened=SimpleNamespace(
                profile_id="profile-1", debugger_address="127.0.0.1:9555",
                ws_endpoint=None,
            ),
            driver=FakeDriver(events),
        )
        with patch.object(roxy_flow, "_load_main_roxy") as loader:
            self.assertEqual(roxy_flow.resolve_roxy_cdp_port("profile-1"), 9555)
        loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
