# -*- coding: utf-8 -*-
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
import protocol_flow
import store
import worker


class ProtocolFlowTests(unittest.TestCase):
    def test_original_roxy_rebind_core_is_deleted(self):
        self.assertFalse(hasattr(worker.roxy_flow, "perform_email_rebind"))
        self.assertFalse(hasattr(worker.roxy_flow, "_change_email_har_guided"))
        self.assertFalse(hasattr(worker.roxy_flow, "_change_email_via_har_api"))
        self.assertEqual(protocol_flow.run_rebind_email.__module__, "rebind_core.pipeline")

    def test_adapter_calls_upstream_run_rebind_email_without_reimplementing_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = Path(tmp) / "login_bundle.json"
            bundle_path.write_text(json.dumps({
                "email": "new@example.com", "access_token": "at-new",
                "auth_session": {"user": {"email": "new@example.com"}},
            }), encoding="utf-8")
            upstream_result = protocol_flow.RebindResult(
                ok=True, old_email="old@example.com", new_email="new@example.com",
                session_email="new@example.com", bundle_path=str(bundle_path),
            )
            progress = []
            with patch.object(protocol_flow, "run_rebind_email", return_value=upstream_result) as run:
                result = protocol_flow.run_upstream_rebind(
                    old_email="old@example.com", new_email="new@example.com",
                    password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                    api_url="https://mail.example/code",
                    proxy_url="http://proxy.example:8080",
                    progress=lambda stage, _message: progress.append(stage),
                )

        run.assert_called_once_with(
            old_email="old@example.com", password="Password!",
            totp_secret="JBSWY3DPEHPK3PXP", new_email="new@example.com",
            mail_api="https://mail.example/code", proxy="http://proxy.example:8080",
            mail_timeout=float(protocol_flow.settings.OTP_MAX_WAIT),
        )
        self.assertEqual(result["access_token"], "at-new")
        self.assertEqual(result["roxy_browser_status"], "not_opened")
        self.assertEqual(result["protocol_engine"], "chatgpt-rebind-standalone/run_rebind_email")
        self.assertEqual(progress, ["protocol_upstream", "protocol_verified"])

    def test_upstream_failure_is_forwarded_with_original_code(self):
        failure = protocol_flow.RebindResult(
            ok=False, code="LOGIN_FAILED", message="email/password/totp_secret 不能为空",
        )
        with patch.object(protocol_flow, "run_rebind_email", return_value=failure), \
                self.assertRaisesRegex(RuntimeError, "LOGIN_FAILED"):
            protocol_flow.run_upstream_rebind(
                old_email="old@example.com", new_email="new@example.com",
                password="", totp_secret="", api_url="https://mail.example/code",
                proxy_url="http://proxy.example:8080",
            )

    def test_invalid_oauth_state_requests_fresh_protocol_session(self):
        failure = protocol_flow.RebindResult(
            ok=False, code="EXPORT_FAILED",
            message="authorize/continue HTTP 409 invalid_state: sign-in session is no longer valid",
        )
        with patch.object(protocol_flow, "run_rebind_email", return_value=failure), \
                self.assertRaises(protocol_flow.ProtocolSessionFailure):
            protocol_flow.run_upstream_rebind(
                old_email="old@example.com", new_email="new@example.com",
                password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                api_url="https://mail.example/code",
                proxy_url="http://proxy.example:8080",
            )

    def test_already_linked_replacement_is_rotated(self):
        failure = protocol_flow.RebindResult(
            ok=False, code="REAUTH_FAILED",
            message='begin 需要 reauth: HTTP 403 {"detail":"Email already linked to another account"}',
        )
        with patch.object(protocol_flow, "run_rebind_email", return_value=failure), \
                self.assertRaisesRegex(protocol_flow.ReplacementEmailFailure, "already linked"):
            protocol_flow.run_upstream_rebind(
                old_email="old@example.com", new_email="new@example.com",
                password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                api_url="https://mail.example/code",
                proxy_url="http://proxy.example:8080",
            )

    def test_rate_limited_authorize_continue_disables_current_proxy(self):
        failure = protocol_flow.RebindResult(
            ok=False, code="EXPORT_FAILED",
            message="authorize/continue rate_limit_exceeded: HTTP 429 Too many requests",
        )
        with patch.object(protocol_flow, "run_rebind_email", return_value=failure), \
                self.assertRaises(protocol_flow.ProxyFailure):
            protocol_flow.run_upstream_rebind(
                old_email="old@example.com", new_email="new@example.com",
                password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                api_url="https://mail.example/code",
                proxy_url="http://proxy.example:8080",
            )

    def test_missing_access_token_rebuilds_protocol_session(self):
        failure = protocol_flow.RebindResult(
            ok=False, code="LOGIN_FAILED", message="登录结果缺少 access_token",
        )
        with patch.object(protocol_flow, "run_rebind_email", return_value=failure), \
                self.assertRaises(protocol_flow.ProtocolSessionFailure):
            protocol_flow.run_upstream_rebind(
                old_email="old@example.com", new_email="new@example.com",
                password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                api_url="https://mail.example/code",
                proxy_url="http://proxy.example:8080",
            )

    def test_protocol_at_refresh_logs_in_once_and_returns_not_opened_result(self):
        login = Mock()
        login.email = "new@example.com"
        login.access_token = "at-refreshed"
        login.session_token = "session-refreshed"
        login.result.email = "new@example.com"
        progress = []
        with patch.object(protocol_flow, "login_with_password_and_totp", return_value=login) as login_call:
            result = protocol_flow.refresh_access_token_protocol(
                email="new@example.com", password="Password!",
                totp_secret="JBSWY3DPEHPK3PXP", proxy_url="http://proxy.example:8080",
                progress=lambda stage, _message: progress.append(stage),
            )

        login_call.assert_called_once_with(
            "new@example.com", "Password!", "JBSWY3DPEHPK3PXP",
            proxy="http://proxy.example:8080",
        )
        self.assertEqual(result["access_token"], "at-refreshed")
        self.assertEqual(result["roxy_browser_status"], "not_opened")
        self.assertEqual(progress, ["protocol_at_refresh", "protocol_at_refreshed"])

    def test_stop_requested_after_upstream_success_does_not_hide_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle_path = Path(tmp) / "login_bundle.json"
            bundle_path.write_text(json.dumps({
                "email": "new@example.com", "access_token": "at-new",
            }), encoding="utf-8")
            upstream_result = protocol_flow.RebindResult(
                ok=True, old_email="old@example.com", new_email="new@example.com",
                session_email="new@example.com", bundle_path=str(bundle_path),
            )
            stop_check = Mock(side_effect=[False, True])
            with patch.object(protocol_flow, "run_rebind_email", return_value=upstream_result):
                result = protocol_flow.run_upstream_rebind(
                    old_email="old@example.com", new_email="new@example.com",
                    password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                    api_url="https://mail.example/code",
                    proxy_url="http://proxy.example:8080",
                    stop_check=stop_check,
                )

        self.assertEqual(result["email"], "new@example.com")
        self.assertEqual(result["access_token"], "at-new")
        self.assertEqual(stop_check.call_count, 1)

    def test_upstream_lock_matches_vendored_source(self):
        lock = json.loads(Path("integrations/upstream-lock.json").read_text(encoding="utf-8"))
        source = next(item for item in lock["runtime_projects"] if item["name"] == "chatgpt-rebind-standalone")
        self.assertEqual(source["commit"], protocol_flow.UPSTREAM_COMMIT)
        self.assertEqual(source["entrypoint"], "rebind_core.pipeline.run_rebind_email")
        root = Path(source["path"])
        self.assertTrue((root / "rebind_core" / "pipeline.py").is_file())
        self.assertTrue((root / "registration_core" / "auth_flow.py").is_file())
        self.assertTrue((root / "README.md").is_file())

        files = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if "__pycache__" in relative or relative.endswith(".pyc"):
                continue
            if relative.startswith("outputs/") and not relative.endswith("/.gitkeep"):
                continue
            files.append((relative, path))
        digest = hashlib.sha256()
        for relative, path in sorted(files):
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        self.assertEqual(len(files), 27)
        self.assertEqual(
            digest.hexdigest(),
            source["tree_sha256"],
        )


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
                    patch.object(worker.protocol_flow, "run_upstream_rebind", return_value={
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
                    patch.object(worker.protocol_flow, "run_upstream_rebind", side_effect=protocol), \
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
