# -*- coding: utf-8 -*-
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
import protocol_flow
import store
import worker


class SourceLoginTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name in ("_ACCOUNTS", "_REPLACEMENTS", "_TASKS", "_PROXIES", "_DETECTION_PROXIES"):
            patcher = patch.object(store, name, Path(tmp.name) / f"{name}.json")
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(worker.settings, "TRANSIENT_RETRY_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        store.import_source_accounts("old@example.com----Password!----https://2fa.example/JBSWY3DPEHPK3PXP")
        store.import_proxies("http://proxy.example:8080\nhttp://proxy2.example:8080")
        self.client = app.create_app(recover=False).test_client()

    def reserve(self, retries=2):
        return store.reserve_login_batch([1], max_transient_retries=retries)[0]

    def result(self, **extra):
        return {"email": "old@example.com", "access_token": "at-original", **extra}

    def test_api_batch_does_not_require_replacement_or_open_browser(self):
        with patch.object(worker, "submit_tasks", side_effect=lambda tasks, workers: len(tasks)) as submit:
            response = self.client.post("/api/login/start", json={
                "account_ids": [1], "workers": 20, "open_roxy_after": True, "rebind_mode": "browser",
            })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual((body["submitted"], body["workers"], body["operation"]), (1, 1, "login"))
        self.assertFalse(body["open_roxy_after"])
        task = submit.call_args.args[0][0]
        self.assertEqual((task["replacement_id"], task["new_email"], task["rebind_mode"]), (0, "", "protocol"))
        self.assertFalse(store._REPLACEMENTS.exists())

    def test_login_worker_uses_original_credentials_and_exports_without_rebinding(self):
        store.import_replacement_emails("unused@example.com----https://mail.example/code")
        before = store._REPLACEMENTS.read_bytes()
        task = self.reserve()
        login = SimpleNamespace(email="old@example.com", access_token="at-original", session_token="session",
                                result=SimpleNamespace(email="old@example.com"))
        def upstream(*args, **kwargs):
            kwargs["progress"]("验证 TOTP")
            return login
        with patch.object(protocol_flow, "login_with_password_and_totp", side_effect=upstream) as call, \
                patch.object(protocol_flow, "run_rebind_email") as rebind, \
                patch.object(worker.browser_rebind, "perform_email_rebind") as browser, \
                patch.object(worker.roxy_flow, "perform_replacement_login") as roxy, \
                patch.object(worker, "submit_trial_check"):
            worker._run(task["id"])
        self.assertEqual(call.call_args.args, ("old@example.com", "Password!", "JBSWY3DPEHPK3PXP"))
        for unused in (rebind, browser, roxy):
            unused.assert_not_called()
        account = store.get_success_account_context(1)
        self.assertEqual(account["current_email"], "old@example.com")
        self.assertFalse(account["email_change_confirmed"])
        self.assertNotIn("new_email", account)
        self.assertNotIn("rebound_at", account)
        self.assertEqual(store._REPLACEMENTS.read_bytes(), before)
        self.assertEqual(store.list_tasks()[0]["stage"], "source_at_saved")
        expected = "old@example.com----Password!----https://2fa.example/JBSWY3DPEHPK3PXP----at-original\n"
        for suffix in ("", "?format=without_old_email"):
            for route in ("/api/export", "/api/accounts/1/export"):
                response = self.client.get(route + suffix)
                self.assertEqual(response.text, expected)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get("/api/accounts/1/access-token").text, "at-original")
        public = self.client.get("/api/state").get_json()["accounts"][0]
        for secret in ("password", "totp_secret", "access_token"):
            self.assertNotIn(secret, public)

    def test_missing_credentials_rejects_entire_batch(self):
        store.import_source_accounts("api@example.com----https://mail.example/otp")
        with patch.object(worker, "submit_tasks") as submit:
            response = self.client.post("/api/login/start", json={"account_ids": [1, 2]})
        self.assertEqual(response.status_code, 400)
        submit.assert_not_called()
        self.assertEqual(store.list_tasks(), [])
        self.assertTrue(all(a["status"] == "ready" for a in store.list_accounts()))

    def test_explicit_selection_and_proxy_required(self):
        for ids in ([], ["bad"], [1, "bad"], "1"):
            with self.subTest(ids=ids):
                self.assertEqual(self.client.post("/api/login/start", json={"account_ids": ids}).status_code, 400)
        self.assertEqual(self.client.post("/api/login/start", json={"account_ids": [999]}).status_code, 409)
        store._write(store._PROXIES, [])
        self.assertEqual(self.client.post("/api/login/start", json={"account_ids": [1]}).status_code, 409)
        self.assertEqual(store.list_tasks(), [])

    def test_concurrent_reservations_do_not_duplicate_account(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(pool.map(lambda _: store.reserve_login_batch([1]), range(2)))
        self.assertEqual(sum(map(len, batches)), 1)

    def test_failure_retry_survives_task_cleanup_without_replacement(self):
        task = self.reserve()
        with patch.object(protocol_flow, "refresh_access_token_protocol", side_effect=RuntimeError("wrong password")) as login:
            worker._run(task["id"])
        self.assertEqual(login.call_count, 1)
        self.assertEqual(store.list_accounts()[0]["status"], "failed")
        self.assertNotIn("new_email", store.list_accounts()[0])
        store.clear_finished_tasks()
        with patch.object(worker, "submit_tasks", return_value=1):
            response = self.client.post("/api/accounts/1/retry", json={})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["task"]["operation"], "login")
        self.assertEqual(store.list_replacements(), [])

    def test_transient_retry_and_proxy_rotation_stay_login_only(self):
        task = self.reserve()
        with patch.object(protocol_flow, "refresh_access_token_protocol", side_effect=[RuntimeError("connection timeout"), self.result()]) as login, \
                patch.object(worker, "submit_trial_check"):
            worker._run(task["id"])
        self.assertEqual(login.call_count, 2)
        self.assertNotEqual(login.call_args_list[0].kwargs["proxy_url"], login.call_args_list[1].kwargs["proxy_url"])
        self.assertEqual(store.list_tasks()[0]["status"], "success")
        self.assertEqual(sum(p["status"] == "failed" for p in store.list_proxies()), 1)

    def test_invalid_session_can_retry_single_proxy_and_exhaust_without_review(self):
        store._write(store._PROXIES, store._read(store._PROXIES)[:1])
        task = self.reserve(retries=1)
        with patch.object(protocol_flow, "refresh_access_token_protocol", side_effect=RuntimeError("invalid_state")) as login:
            worker._run(task["id"])
        self.assertEqual(login.call_count, 2)
        self.assertEqual(store.list_accounts()[0]["status"], "failed")
        self.assertEqual(store.list_tasks()[0]["stage"], "failed")
        self.assertEqual(store.list_replacements(), [])

    def test_stop_during_login_and_restart_preserve_original_identity(self):
        task = self.reserve()
        def stop(**kwargs):
            store.request_task_stop(task["id"])
            kwargs["progress"]("protocol_at_refreshed", "received")
            return self.result()
        with patch.object(protocol_flow, "refresh_access_token_protocol", side_effect=stop):
            worker._run(task["id"])
        self.assertEqual(store.list_tasks()[0]["status"], "stopped")
        self.assertEqual(store.list_accounts()[0]["status"], "ready")
        self.reserve()
        self.assertEqual(store.recover_interrupted_tasks(), 1)
        account = store.list_accounts()[0]
        self.assertEqual(account["status"], "failed")
        self.assertFalse(account.get("email_change_confirmed"))
        self.assertNotIn("new_email", account)

    def test_missing_token_and_wrong_email_cannot_be_success(self):
        for result in (self.result(access_token=""), self.result(email="other@example.com")):
            with self.subTest(result=result):
                task = self.reserve()
                with patch.object(protocol_flow, "refresh_access_token_protocol", return_value=result):
                    worker._run(task["id"])
                self.assertEqual(store.list_accounts()[0]["status"], "failed")
                self.assertIsNone(store.export_success_line(1))

    def test_mixed_export_formats_preserve_password_and_login_email(self):
        task = self.reserve()
        store.finish_success(task["id"], self.result())
        store.import_source_accounts("second@example.com----Password!----JBSWY3DPEHPK3PXP")
        store.import_replacement_emails("new@example.com----https://mail.example/code")
        task2 = store.reserve_batch([2])[0]
        rows = store._read(store._ACCOUNTS)
        rows[1]["password"] = "pass----with@example.com----separator"
        store._write(store._ACCOUNTS, rows)
        store.finish_success(task2["id"], {"email": "new@example.com", "access_token": "at-new"})
        full = store.export_success_lines()
        short = self.client.get("/api/export?format=without_old_email").text.splitlines()
        self.assertEqual(short[0], full[0])
        self.assertEqual(short[1], full[1].split("----", 1)[1])
        self.assertIn("pass----with@example.com----separator", short[1])

    def test_switching_from_login_failure_to_rebind_restores_retry_mode(self):
        task = self.reserve()
        store.finish_failure(task["id"], "login failed")
        store.import_replacement_emails("new@example.com----https://mail.example/code")
        rebind = store.reserve_batch([1])[0]
        store.finish_failure(rebind["id"], "rebind failed")
        retry = store.reserve_failed_account_retry(1)["task"]
        self.assertNotEqual(retry.get("operation"), "login")
        self.assertNotEqual(retry["replacement_id"], 0)

    def test_success_refresh_uses_original_email(self):
        task = self.reserve()
        store.finish_success(task["id"], self.result())
        with patch.object(protocol_flow, "refresh_access_token_protocol", return_value=self.result(access_token="at-refreshed")) as login, \
                patch.object(worker, "submit_trial_check"):
            worker._refresh_access_token(1)
        self.assertEqual(login.call_args.kwargs["email"], "old@example.com")
        self.assertEqual(store.get_success_access_token(1), "at-refreshed")
        self.assertEqual(len(store.export_success_line(1).split("----")), 4)


if __name__ == "__main__":
    unittest.main()
