# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
import protocol_flow
import store
import worker


class ProtocolFlowTests(unittest.TestCase):
    def test_vendored_protocol_runs_login_begin_verify_and_relogin_without_roxy(self):
        login_old = SimpleNamespace()
        login_new = SimpleNamespace()
        client = Mock()
        client.eligibility.return_value = {"eligible": True, "eligibility_type": "password"}
        progress = []
        with patch.object(protocol_flow, "_login", side_effect=[login_old, login_new]) as login, \
                patch.object(protocol_flow, "ChangeEmailClient", return_value=client), \
                patch.object(protocol_flow, "wait_code", return_value="123456") as wait, \
                patch.object(protocol_flow, "build_login_bundle", return_value={
                    "email": "new@example.com", "access_token": "at-new",
                    "auth_session": {"user": {"email": "new@example.com"}},
                }):
            result = protocol_flow.perform_email_rebind(
                old_email="old@example.com", new_email="new@example.com",
                password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                api_url="https://mail.example/code",
                proxy_url="http://proxy.example:8080",
                progress=lambda stage, _message: progress.append(stage),
            )

        self.assertEqual(login.call_count, 2)
        client.eligibility.assert_called_once_with()
        client.begin.assert_called_once_with("new@example.com")
        client.verify.assert_called_once_with("new@example.com", "123456")
        self.assertEqual(wait.call_args.kwargs["poll_interval"], protocol_flow.settings.OTP_POLL_INTERVAL)
        self.assertEqual(result["access_token"], "at-new")
        self.assertEqual(result["roxy_browser_status"], "not_opened")
        self.assertEqual(result["protocol_engine"], "chatgpt-rebind-standalone")
        self.assertIn("protocol_login_old", progress)
        self.assertIn("protocol_verified", progress)

    def test_protocol_requires_password_and_totp(self):
        with self.assertRaisesRegex(RuntimeError, "密码和 2FA"):
            protocol_flow.perform_email_rebind(
                old_email="old@example.com", new_email="new@example.com",
                password="", totp_secret="", api_url="https://mail.example/code",
                proxy_url="http://proxy.example:8080",
            )

    def test_upstream_lock_matches_vendored_source(self):
        lock = json.loads(Path("integrations/upstream-lock.json").read_text(encoding="utf-8"))
        source = next(item for item in lock["runtime_projects"] if item["name"] == "chatgpt-rebind-standalone")
        self.assertEqual(source["commit"], protocol_flow.UPSTREAM_COMMIT)
        root = Path(source["path"])
        self.assertTrue((root / "rebind_core" / "pipeline.py").is_file())
        self.assertTrue((root / "registration_core" / "auth_flow.py").is_file())
        self.assertTrue((root / "README.md").is_file())


class ProtocolWorkerTests(unittest.TestCase):
    def _storage(self, root: Path):
        return (
            patch.object(store, "_ACCOUNTS", root / "accounts.json"),
            patch.object(store, "_REPLACEMENTS", root / "replacements.json"),
            patch.object(store, "_TASKS", root / "tasks.json"),
            patch.object(store, "_PROXIES", root / "proxies.json"),
        )

    def test_default_protocol_mode_never_opens_roxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches = self._storage(root)
            fake_random = Mock()
            fake_random.choice.side_effect = lambda rows: rows[0]
            with patches[0], patches[1], patches[2], patches[3], \
                    patch.object(store, "_PROXY_RANDOM", fake_random), \
                    patch.object(worker.protocol_flow, "perform_email_rebind", return_value={
                        "email": "new@example.com", "access_token": "at-new",
                        "protocol_engine": "chatgpt-rebind-standalone",
                        "protocol_upstream_commit": protocol_flow.UPSTREAM_COMMIT,
                        "roxy_browser_status": "not_opened",
                    }), \
                    patch.object(worker.roxy_flow, "perform_replacement_login") as open_roxy:
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://protocol.example:8080")
                worker._run(store.reserve_batch(open_roxy_after=False)[0]["id"])

                open_roxy.assert_not_called()
                account = store.list_accounts()[0]
                self.assertEqual(account["status"], "success")
                self.assertEqual(account["roxy_browser_status"], "not_opened")
                self.assertFalse(account["roxy_open_requested"])
                self.assertTrue(store.delete_source_account(account["id"])["deleted"])

    def test_enabled_extension_uses_a_distinct_second_proxy_for_roxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches = self._storage(root)
            fake_random = Mock()
            fake_random.choice.side_effect = lambda rows: rows[0]
            protocol_urls = []
            roxy_urls = []

            def protocol(**kwargs):
                protocol_urls.append(kwargs["proxy_url"])
                return {
                    "email": "new@example.com", "access_token": "at-protocol",
                    "protocol_engine": "chatgpt-rebind-standalone",
                    "protocol_upstream_commit": protocol_flow.UPSTREAM_COMMIT,
                    "roxy_browser_status": "not_opened",
                }

            def open_roxy(**kwargs):
                roxy_urls.append(kwargs["proxy_url"])
                kwargs["proxy_verified"]({"ip": "203.0.113.22", "country": "US"})
                return {
                    "email": "new@example.com", "access_token": "at-roxy",
                    "roxy_profile_id": "profile-2", "roxy_cdp_port": 9444,
                    "roxy_browser_status": "open",
                }

            with patches[0], patches[1], patches[2], patches[3], \
                    patch.object(store, "_PROXY_RANDOM", fake_random), \
                    patch.object(worker.protocol_flow, "perform_email_rebind", side_effect=protocol), \
                    patch.object(worker.roxy_flow, "perform_replacement_login", side_effect=open_roxy):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://protocol.example:8080\nhttp://roxy.example:8080")
                task = store.reserve_batch(open_roxy_after=True)[0]
                worker._run(task["id"])

                self.assertEqual(len(protocol_urls), 1)
                self.assertEqual(len(roxy_urls), 1)
                self.assertNotEqual(protocol_urls[0], roxy_urls[0])
                account = store.list_accounts()[0]
                self.assertEqual(account["roxy_browser_status"], "open")
                self.assertEqual(account["roxy_profile_id"], "profile-2")
                self.assertEqual(account["roxy_cdp_port"], 9444)
                self.assertTrue(account["roxy_open_requested"])
                persisted_task = store.list_tasks()[0]
                self.assertNotEqual(persisted_task["proxy_id"], persisted_task["roxy_proxy_id"])

    def test_start_route_requires_second_proxy_only_when_roxy_extension_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches = self._storage(root)
            with patches[0], patches[1], patches[2], patches[3], \
                    patch.object(worker, "submit_tasks", return_value=1):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://protocol.example:8080")
                client = app.create_app(recover=False).test_client()

                blocked = client.post("/api/rebind/start", json={
                    "account_ids": [], "workers": 1, "open_roxy_after": True,
                })
                self.assertEqual(blocked.status_code, 409)
                self.assertIn("至少两条", blocked.get_json()["error"])

                store.import_proxies("http://roxy.example:8080")
                started = client.post("/api/rebind/start", json={
                    "account_ids": [], "workers": 1, "open_roxy_after": True,
                })
                self.assertEqual(started.status_code, 200)
                self.assertTrue(started.get_json()["open_roxy_after"])
                self.assertTrue(store.list_tasks()[0]["open_roxy_after"])


if __name__ == "__main__":
    unittest.main()
