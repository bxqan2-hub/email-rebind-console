# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import roxy_flow
import store
import worker


class WorkerRotationTests(unittest.TestCase):
    def test_invalid_totp_material_is_not_auto_retried(self):
        self.assertFalse(
            worker._should_auto_retry(
                {"stage": "protocol_login_old"},
                ValueError("2FA 内容不是有效的 Base32 Secret"),
            )
        )

    def test_transient_failure_retries_same_replacement_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_random = Mock()
            fake_random.choice.side_effect = lambda rows: rows[0]
            seen: list[str] = []

            def perform(**kwargs):
                seen.append(kwargs["proxy_url"])
                if len(seen) == 1:
                    kwargs["progress"]("submit_new_email", "提交替换邮箱并请求新邮箱验证码")
                    raise RuntimeError("提交替换邮箱后未取得服务端响应，可使用失败重试")
                return {"email": kwargs["new_email"], "access_token": "at-new"}

            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(store, "_PROXY_RANDOM", fake_random), \
                    patch.object(worker.settings, "MAX_TRANSIENT_RETRIES", 1), \
                    patch.object(worker.settings, "TRANSIENT_RETRY_DELAY", 0), \
                    patch.object(worker.settings, "MAX_PROXY_ATTEMPTS", 5), \
                    patch.object(worker.protocol_flow, "run_upstream_rebind", side_effect=perform) as run:
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://first.example:8080\nhttp://second.example:8080")
                initial = store.reserve_batch()[0]
                worker._run(initial["id"])

                self.assertEqual(run.call_count, 2)
                self.assertNotEqual(seen[0], seen[1])
                self.assertEqual(store.list_accounts()[0]["status"], "success")
                self.assertEqual(store.list_replacements()[0]["status"], "used")
                tasks = sorted(store.list_tasks(), key=lambda row: int(row["id"]))
                self.assertEqual(tasks[0]["stage"], "transient_failed")
                self.assertEqual(tasks[0]["auto_retry_status"], "scheduled")
                self.assertEqual(tasks[1]["stage"], "protocol_verified")
                self.assertEqual(tasks[1]["retry_of_task_id"], tasks[0]["id"])
                self.assertEqual(tasks[1]["attempt"], 2)

    def test_begin_no_response_retries_after_an_earlier_proxy_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_random = Mock()
            fake_random.choice.side_effect = lambda rows: rows[0]
            seen: list[str] = []

            def perform(**kwargs):
                seen.append(kwargs["proxy_url"])
                if len(seen) == 1:
                    raise roxy_flow.ProxyFailure("Roxy 窗口代理出口复核失败；登录尚未开始")
                if len(seen) == 2:
                    kwargs["progress"]("submit_new_email", "提交替换邮箱并请求新邮箱验证码")
                    raise RuntimeError("提交替换邮箱后未取得服务端响应，可使用失败重试")
                return {"email": kwargs["new_email"], "access_token": "at-new"}

            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(store, "_PROXY_RANDOM", fake_random), \
                    patch.object(worker.settings, "MAX_TRANSIENT_RETRIES", 1), \
                    patch.object(worker.settings, "TRANSIENT_RETRY_DELAY", 0), \
                    patch.object(worker.settings, "MAX_PROXY_ATTEMPTS", 5), \
                    patch.object(worker.protocol_flow, "run_upstream_rebind", side_effect=perform) as run:
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies(
                    "http://first.example:8080\n"
                    "http://second.example:8080\n"
                    "http://third.example:8080"
                )
                initial = store.reserve_batch()[0]
                worker._run(initial["id"])

                self.assertEqual(run.call_count, 3)
                self.assertEqual(len(set(seen)), 3)
                self.assertEqual(store.list_accounts()[0]["status"], "success")
                tasks = sorted(store.list_tasks(), key=lambda row: int(row["id"]))
                self.assertEqual(tasks[0]["stage"], "transient_failed")
                self.assertEqual(tasks[0]["auto_retry_status"], "scheduled")
                self.assertEqual(tasks[1]["retry_of_task_id"], tasks[0]["id"])
                self.assertEqual(tasks[1]["stage"], "protocol_verified")

    def test_upstream_temporary_failure_retries_as_a_new_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_random = Mock()
            fake_random.choice.side_effect = lambda rows: rows[0]
            seen: list[str] = []

            def perform(**kwargs):
                seen.append(kwargs["proxy_url"])
                if len(seen) == 1:
                    kwargs["progress"]("check_email_eligibility", "检查当前账号的邮箱换绑资格")
                    raise RuntimeError("上游换绑资格接口临时不可用")
                return {"email": kwargs["new_email"], "access_token": "at-new"}

            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(store, "_PROXY_RANDOM", fake_random), \
                    patch.object(worker.settings, "MAX_TRANSIENT_RETRIES", 1), \
                    patch.object(worker.settings, "TRANSIENT_RETRY_DELAY", 0), \
                    patch.object(worker.settings, "MAX_PROXY_ATTEMPTS", 5), \
                    patch.object(worker.protocol_flow, "run_upstream_rebind", side_effect=perform) as run:
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://first.example:8080\nhttp://second.example:8080")
                worker._run(store.reserve_batch()[0]["id"])

                self.assertEqual(run.call_count, 2)
                self.assertNotEqual(seen[0], seen[1])
                tasks = sorted(store.list_tasks(), key=lambda row: int(row["id"]))
                self.assertEqual(tasks[0]["stage"], "transient_failed")
                self.assertEqual(tasks[1]["retry_of_task_id"], tasks[0]["id"])
                self.assertEqual(tasks[1]["stage"], "protocol_verified")

    def test_invalid_oauth_state_switches_proxy_without_disabling_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_random = Mock()
            fake_random.choice.side_effect = lambda rows: rows[0]
            seen = []

            def perform(**kwargs):
                seen.append(kwargs["proxy_url"])
                if len(seen) == 1:
                    raise worker.protocol_flow.ProtocolSessionFailure("HTTP 409 invalid_state")
                return {"email": kwargs["new_email"], "access_token": "at-new"}

            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(store, "_PROXY_RANDOM", fake_random), \
                    patch.object(worker.settings, "MAX_PROXY_ATTEMPTS", 5), \
                    patch.object(worker.protocol_flow, "run_upstream_rebind", side_effect=perform):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://first.example:8080\nhttp://second.example:8080")

                worker._run(store.reserve_batch()[0]["id"])

                self.assertEqual(len(seen), 2)
                self.assertNotEqual(seen[0], seen[1])
                self.assertTrue(all(row["status"] == "available" for row in store.list_proxies()))
                self.assertEqual(store.list_accounts()[0]["status"], "success")

    def test_transient_retries_are_bounded_and_release_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_random = Mock()
            fake_random.choice.side_effect = lambda rows: rows[0]
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(store, "_PROXY_RANDOM", fake_random), \
                    patch.object(worker.settings, "MAX_TRANSIENT_RETRIES", 1), \
                    patch.object(worker.settings, "TRANSIENT_RETRY_DELAY", 0), \
                    patch.object(worker.protocol_flow, "run_upstream_rebind", side_effect=TimeoutError("账号登录超时")) as run:
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://first.example:8080\nhttp://second.example:8080")
                worker._run(store.reserve_batch()[0]["id"])

                self.assertEqual(run.call_count, 2)
                self.assertEqual(store.list_accounts()[0]["status"], "failed")
                self.assertEqual(store.list_replacements()[0]["status"], "available")
                final_task = sorted(store.list_tasks(), key=lambda row: int(row["id"]))[-1]
                self.assertEqual(final_task["auto_retry_status"], "exhausted")

    def test_otp_failure_marks_email_and_automatically_uses_next_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = (
                patch.object(store, "_ACCOUNTS", root / "accounts.json"),
                patch.object(store, "_REPLACEMENTS", root / "replacements.json"),
                patch.object(store, "_TASKS", root / "tasks.json"),
                patch.object(store, "_PROXIES", root / "proxies.json"),
            )
            with storage[0], storage[1], storage[2], storage[3], patch.object(worker.settings, "MAX_REPLACEMENT_ATTEMPTS", 5), patch.object(worker.settings, "MAX_PROXY_ATTEMPTS", 5), patch.object(
                worker.protocol_flow,
                "run_upstream_rebind",
                side_effect=[
                    roxy_flow.ReplacementEmailFailure("otp_unavailable", "bad@example.com 收不到验证码"),
                    {"email": "good@example.com", "access_token": "at-new"},
                ],
            ) as perform:
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails(
                    "bad@example.com----https://mail.example/bad\n"
                    "good@example.com----https://mail.example/good"
                )
                store.import_proxies("http://proxy.example:8080")
                initial = store.reserve_batch()[0]
                worker._run(initial["id"])

                self.assertEqual(perform.call_count, 2)
                self.assertNotIn("source_api_url", perform.call_args_list[0].kwargs)
                replacements = {row["email"]: row for row in store.list_replacements()}
                self.assertEqual(replacements["bad@example.com"]["status"], "failed")
                self.assertEqual(replacements["bad@example.com"]["failure_code"], "otp_unavailable")
                self.assertEqual(replacements["good@example.com"]["status"], "used")
                account = store.list_accounts()[0]
                self.assertEqual(account["status"], "success")
                self.assertEqual(account["current_email"], "good@example.com")
                tasks = list(reversed(store.list_tasks()))
                self.assertEqual(len(tasks), 2)
                self.assertEqual(tasks[0]["next_task_id"], tasks[1]["id"])
                self.assertEqual(tasks[1]["retry_of_task_id"], tasks[0]["id"])
                self.assertEqual(tasks[1]["attempt"], 2)

    def test_worker_passes_password_totp_and_replacement_api_to_protocol_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(worker.settings, "MAX_PROXY_ATTEMPTS", 5), \
                    patch.object(worker.protocol_flow, "run_upstream_rebind", return_value={
                        "email": "new@example.com", "access_token": "at-new",
                    }) as perform:
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/new")
                store.import_proxies("http://proxy.example:8080")
                worker._run(store.reserve_batch()[0]["id"])

                kwargs = perform.call_args.kwargs
                self.assertEqual(kwargs["api_url"], "https://mail.example/new")
                self.assertEqual(kwargs["password"], "Password!")
                self.assertEqual(kwargs["totp_secret"], "JBSWY3DPEHPK3PXP")

    def test_worker_honors_stop_request_without_starting_roxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = (
                patch.object(store, "_ACCOUNTS", root / "accounts.json"),
                patch.object(store, "_REPLACEMENTS", root / "replacements.json"),
                patch.object(store, "_TASKS", root / "tasks.json"),
                patch.object(store, "_PROXIES", root / "proxies.json"),
            )
            with storage[0], storage[1], storage[2], storage[3], \
                    patch.object(worker.protocol_flow, "run_upstream_rebind") as perform:
                store.import_source_accounts("old@example.com----https://mail.example/old")
                store.import_replacement_emails("new@example.com----https://mail.example/new")
                task = store.reserve_batch()[0]
                store.request_task_stop(task["id"])

                worker._run(task["id"])

                perform.assert_not_called()
                self.assertEqual(store.list_tasks()[0]["status"], "stopped")
                self.assertEqual(store.list_accounts()[0]["status"], "ready")

    def test_review_login_task_uses_existing_replacement_and_finishes_successfully(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(worker.roxy_flow, "perform_replacement_login", return_value={
                        "email": "new@example.com", "access_token": "at-new", "roxy_profile_id": "profile-1",
                    }) as perform:
                store.import_source_accounts("old@example.com----https://mail.example/old")
                store.import_replacement_emails("new@example.com----https://mail.example/new")
                store.import_proxies("http://proxy.example:8080")
                initial = store.reserve_batch()[0]
                store.finish_review_failure(initial["id"], "new@example.com", "AT 刷新失败")
                retry = store.reserve_review_login_retry(1, max_transient_retries=1)["task"]

                worker._run(retry["id"])

                self.assertEqual(store.list_accounts()[0]["status"], "success")
                self.assertEqual(store.list_replacements()[0]["status"], "used")
                kwargs = perform.call_args.kwargs
                self.assertEqual(kwargs["new_email"], "new@example.com")
                self.assertEqual(kwargs["max_relogin_retries"], 1)

    def test_pool_exhaustion_keeps_account_failed_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"), patch.object(store, "_PROXIES", root / "proxies.json"), patch.object(
                worker.protocol_flow,
                "run_upstream_rebind",
                side_effect=roxy_flow.ReplacementEmailFailure("otp_unavailable", "没有验证码"),
            ):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("bad@example.com----https://mail.example/bad")
                store.import_proxies("http://proxy.example:8080")
                initial = store.reserve_batch()[0]
                worker._run(initial["id"])
                account = store.list_accounts()[0]
                self.assertEqual(account["status"], "failed")
                self.assertIn("号池没有更多可用邮箱", account["error"])
                self.assertEqual(store.list_replacements()[0]["status"], "failed")

    def test_unknown_post_change_result_is_frozen_for_manual_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"), patch.object(store, "_PROXIES", root / "proxies.json"), patch.object(
                worker.protocol_flow,
                "run_upstream_rebind",
                side_effect=roxy_flow.RebindOutcomeUnknown("maybe@example.com", "验证码已提交但 AT 刷新失败"),
            ):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("maybe@example.com----https://mail.example/maybe")
                store.import_proxies("http://proxy.example:8080")
                initial = store.reserve_batch()[0]
                worker._run(initial["id"])
                account = store.list_accounts()[0]
                replacement = store.list_replacements()[0]
                task = store.list_tasks()[0]
                self.assertEqual(account["status"], "review")
                self.assertEqual(account["current_email"], "maybe@example.com")
                self.assertTrue(account["email_change_uncertain"])
                self.assertEqual(replacement["status"], "review")
                self.assertEqual(task["stage"], "manual_review")
                self.assertFalse(task["retryable"])

    def test_success_account_refresh_updates_exported_access_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(worker.roxy_flow, "refresh_retained_access_token", return_value={
                        "email": "new@example.com", "access_token": "at-plus",
                        "roxy_profile_id": "profile-1",
                    }) as refresh:
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                task = store.reserve_batch()[0]
                store.finish_success(task["id"], {
                    "email": "new@example.com", "access_token": "at-old",
                    "roxy_profile_id": "profile-1", "roxy_browser_status": "open",
                })
                account_id = store.list_accounts()[0]["id"]
                self.assertIsNotNone(store.begin_access_token_refresh(account_id))
                worker._refresh_access_token(account_id)

                refresh.assert_called_once_with("profile-1", "new@example.com")
                self.assertEqual(
                    store.export_success_lines(),
                    ["old@example.com----new@example.com----Password!----JBSWY3DPEHPK3PXP----at-plus"],
                )
                self.assertEqual(store.list_accounts()[0]["roxy_browser_status"], "open")

    def test_same_roxy_token_must_pass_validity_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            token = "header.payload.signature"
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(worker.roxy_flow, "refresh_retained_access_token", return_value={
                        "email": "new@example.com", "access_token": token,
                        "roxy_profile_id": "profile-1",
                    }), \
                    patch.object(worker.trial_check, "check_access_token_validity", return_value={
                        "outcome": "invalid_confirmed", "valid": False, "error": "HTTP 401",
                    }) as validity, \
                    patch.object(worker, "submit_trial_check") as submit_trial, \
                    patch.object(worker.roxy_flow, "retained_profile_is_connected", return_value=True):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                task = store.reserve_batch()[0]
                store.finish_success(task["id"], {
                    "email": "new@example.com", "access_token": token,
                    "roxy_profile_id": "profile-1", "roxy_browser_status": "open",
                })
                account_id = store.list_accounts()[0]["id"]
                self.assertIsNotNone(store.begin_access_token_refresh(account_id))

                worker._refresh_access_token(account_id)

                account = store.list_accounts()[0]
                validity.assert_called_once_with(token)
                submit_trial.assert_not_called()
                self.assertEqual(account["at_refresh_status"], "failed")
                self.assertIn("未通过有效性确认", account["at_refresh_error"])

    def test_success_account_refresh_without_roxy_uses_one_protocol_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(worker.protocol_flow, "refresh_access_token_protocol", return_value={
                        "email": "new@example.com", "access_token": "at-protocol-refresh",
                        "roxy_profile_id": "", "roxy_browser_status": "not_opened",
                    }) as refresh_protocol, \
                    patch.object(worker.roxy_flow, "refresh_retained_access_token") as refresh_roxy:
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://proxy.example:8080")
                task = store.reserve_batch()[0]
                store.finish_success(task["id"], {
                    "email": "new@example.com", "access_token": "at-old",
                    "roxy_profile_id": "", "roxy_browser_status": "not_opened",
                })
                account_id = store.list_accounts()[0]["id"]
                self.assertIsNotNone(store.begin_access_token_refresh(account_id))
                worker._refresh_access_token(account_id)

                refresh_roxy.assert_not_called()
                refresh_protocol.assert_called_once()
                kwargs = refresh_protocol.call_args.kwargs
                self.assertEqual(kwargs["email"], "new@example.com")
                self.assertEqual(kwargs["password"], "Password!")
                self.assertEqual(kwargs["totp_secret"], "JBSWY3DPEHPK3PXP")
                self.assertEqual(kwargs["proxy_url"], "http://proxy.example:8080")
                account = store.list_accounts()[0]
                self.assertEqual(store.get_success_access_token(account_id), "at-protocol-refresh")
                self.assertEqual(account["roxy_browser_status"], "not_opened")
                self.assertFalse(account.get("roxy_profile_id"))

    def test_worker_extracts_totp_secret_from_2fa_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(worker.settings, "MAX_PROXY_ATTEMPTS", 5), \
                    patch.object(worker.protocol_flow, "run_upstream_rebind", return_value={
                        "email": "new@example.com", "access_token": "at-new",
                    }) as perform:
                store.import_source_accounts(
                    "old@example.com----Password!----"
                    "https://2fa.example/JBSWY3DPEHPK3PXP"
                )
                store.import_replacement_emails("new@example.com----https://mail.example/new")
                store.import_proxies("http://proxy.example:8080")

                worker._run(store.reserve_batch()[0]["id"])

                self.assertEqual(
                    perform.call_args.kwargs["totp_secret"],
                    "JBSWY3DPEHPK3PXP",
                )


if __name__ == "__main__":
    unittest.main()
