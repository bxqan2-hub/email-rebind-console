# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store


class StoreTests(unittest.TestCase):
    def test_authenticated_proxy_formats_default_to_socks5h(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_PROXIES", root / "proxies.json"):
                result = store.import_proxies(
                    "proxy-one.example:3000:user-a:pass-a\n"
                    "proxy-two.example:3001----user-b----pass-b"
                )

                self.assertEqual(result["parsed"], 2)
                self.assertEqual(
                    [row["display"] for row in store.list_proxies()],
                    [
                        "socks5h://***:***@proxy-one.example:3000",
                        "socks5h://***:***@proxy-two.example:3001",
                    ],
                )

    def test_source_import_keeps_email_url_in_original_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"):
                result = store.import_source_accounts(
                    "old@example.com----https://mail.example/old-code\n"
                    "old2@example.com----Password!----JBSWY3DPEHPK3PXP\n"
                    "bad input"
                )

                self.assertEqual(result["parsed"], 2)
                self.assertEqual(result["inserted"], 2)
                self.assertEqual(result["invalid"], [{
                    "line": 3,
                    "reason": "原邮箱需要：邮箱----http(s)://API取码地址，或 邮箱----密码----MFA Secret",
                }])
                accounts = store.list_accounts()
                self.assertEqual([row["old_email"] for row in accounts], ["old@example.com", "old2@example.com"])
                self.assertTrue(accounts[0]["has_api"])
                self.assertFalse(accounts[0]["has_password"])
                self.assertFalse(accounts[0]["has_totp"])
                self.assertFalse(accounts[1]["has_api"])
                self.assertTrue(accounts[1]["has_password"])
                self.assertTrue(accounts[1]["has_totp"])
                self.assertNotIn("api_url", accounts[0])
                self.assertEqual(store.list_replacements(), [])

    def test_reimport_can_switch_original_account_login_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"):
                store.import_source_accounts("mail@example.com----Password!----JBSWY3DPEHPK3PXP")
                result = store.import_source_accounts("mail@example.com====https://mail.example/api?email={email}")

                self.assertEqual(result["parsed"], 1)
                self.assertEqual(result["updated"], 1)
                account = store.list_accounts()[0]
                self.assertTrue(account["has_api"])
                self.assertFalse(account["has_password"])
                self.assertFalse(account["has_totp"])

    def test_delete_replacement_and_proxy_when_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"):
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://proxy.example:8080")
                replacement_id = store.list_replacements()[0]["id"]
                proxy_id = store.list_proxies()[0]["id"]

                self.assertTrue(store.delete_replacement(replacement_id)["deleted"])
                self.assertTrue(store.delete_proxy(proxy_id)["deleted"])
                self.assertEqual(store.list_replacements(), [])
                self.assertEqual(store.list_proxies(), [])
                self.assertEqual(store.delete_replacement(replacement_id)["reason"], "not_found")
                self.assertEqual(store.delete_proxy(proxy_id)["reason"], "not_found")

    def test_delete_original_account_when_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"):
                store.import_source_accounts("old@example.com----https://mail.example/old")
                account_id = store.list_accounts()[0]["id"]

                self.assertTrue(store.delete_source_account(account_id)["deleted"])
                self.assertEqual(store.list_accounts(), [])
                self.assertEqual(store.delete_source_account(account_id)["reason"], "not_found")

    def test_delete_original_account_is_blocked_during_active_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"):
                store.import_source_accounts("old@example.com----https://mail.example/old")
                store.import_replacement_emails("new@example.com----https://mail.example/new")
                task = store.reserve_batch()[0]

                result = store.delete_source_account(task["account_id"])
                self.assertEqual(result["reason"], "in_use")
                self.assertEqual(result["task_id"], task["id"])
                self.assertEqual(len(store.list_accounts()), 1)

    def test_delete_replacement_and_proxy_is_blocked_during_active_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://proxy.example:8080")
                task = store.reserve_batch()[0]
                proxy = store.pick_random_proxy()
                store.assign_task_proxy(task["id"], proxy, 1)

                replacement_id = store.list_replacements()[0]["id"]
                proxy_id = store.list_proxies()[0]["id"]
                replacement_result = store.delete_replacement(replacement_id)
                proxy_result = store.delete_proxy(proxy_id)
                self.assertEqual(replacement_result["reason"], "in_use")
                self.assertEqual(proxy_result["reason"], "in_use")
                self.assertEqual(replacement_result["task_id"], task["id"])
                self.assertEqual(proxy_result["task_id"], task["id"])

    def test_main_format_pair_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"), patch.object(store, "_PROXIES", root / "proxies.json"):
                imported = store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                pool = store.import_replacement_emails("new@example.com----https://mail.example/code")
                self.assertEqual(imported["inserted"], 1)
                self.assertEqual(pool["inserted"], 1)
                tasks = store.reserve_batch()
                self.assertEqual(tasks[0]["old_email"], "old@example.com")
                self.assertEqual(tasks[0]["new_email"], "new@example.com")
                store.finish_success(tasks[0]["id"], {
                    "email": "new@example.com", "access_token": "at-new",
                    "roxy_profile_id": "profile-1", "roxy_browser_status": "open",
                })
                self.assertEqual(
                    store.export_success_lines(),
                    ["old@example.com----new@example.com----Password!----JBSWY3DPEHPK3PXP----at-new"],
                )
                account = store.list_accounts()[0]
                self.assertEqual(
                    store.export_success_line(account["id"]),
                    "old@example.com----new@example.com----Password!----JBSWY3DPEHPK3PXP----at-new",
                )
                self.assertEqual(account["roxy_browser_status"], "open")
                self.assertEqual(account["roxy_profile_id"], "profile-1")
                self.assertEqual(store.summary()["roxy_open"], 1)

                replacement_id = store.list_replacements()[0]["id"]
                self.assertTrue(store.delete_replacement(replacement_id)["deleted"])
                self.assertEqual(store.list_replacements(), [])
                self.assertEqual(store.list_tasks()[0]["status"], "success")
                self.assertEqual(
                    store.export_success_lines(),
                    ["old@example.com----new@example.com----Password!----JBSWY3DPEHPK3PXP----at-new"],
                )

                started = store.begin_access_token_refresh(account["id"])
                self.assertEqual(started["at_refresh_status"], "running")
                refreshed = store.finish_access_token_refresh(account["id"], {"access_token": "at-plus"})
                self.assertEqual(refreshed["at_refresh_status"], "success")
                self.assertTrue(refreshed["at_token_changed"])
                self.assertEqual(
                    store.export_success_lines(),
                    ["old@example.com----new@example.com----Password!----JBSWY3DPEHPK3PXP----at-plus"],
                )
                restarted = store.begin_access_token_refresh(account["id"])
                self.assertNotIn("at_token_changed", restarted)
                unchanged = store.finish_access_token_refresh(account["id"], {"access_token": "at-plus"})
                self.assertFalse(unchanged["at_token_changed"])
                self.assertEqual(
                    store.export_success_lines(),
                    ["old@example.com----new@example.com----Password!----JBSWY3DPEHPK3PXP----at-plus"],
                )
                closed = store.mark_roxy_profile_closed(account["id"])
                self.assertEqual(closed["roxy_browser_status"], "closed")
                self.assertEqual(store.summary()["roxy_open"], 0)

    def test_email_api_source_is_exported_with_original_email_and_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"):
                store.import_source_accounts("old@example.com----https://mail.example/old")
                store.import_replacement_emails("new@example.com----https://mail.example/new")
                task = store.reserve_batch()[0]
                store.finish_success(task["id"], {"email": "new@example.com", "access_token": "at-new"})

                self.assertEqual(
                    store.export_success_lines(),
                    ["old@example.com----new@example.com----https://mail.example/new----at-new"],
                )
                account = store.list_accounts()[0]
                self.assertTrue(account["has_replacement_api"])
                self.assertTrue(store.delete_replacement(task["replacement_id"])["deleted"])
                self.assertEqual(
                    store.export_success_lines(),
                    ["old@example.com----new@example.com----https://mail.example/new----at-new"],
                )

    def test_failed_task_logs_can_be_cleared_without_removing_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"):
                store.import_source_accounts("first@example.com----Password!----JBSWY3DPEHPK3PXP\nsecond@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new1@example.com----https://mail.example/1\nnew2@example.com----https://mail.example/2")
                tasks = store.reserve_batch()
                store.finish_failure(tasks[0]["id"], "failed")
                store.finish_success(tasks[1]["id"], {"email": "new2@example.com", "access_token": "at-2"})

                blocked = store.delete_failed_task(tasks[1]["id"])
                self.assertEqual(blocked["reason"], "not_failed")
                removed = store.delete_failed_task(tasks[0]["id"])
                self.assertTrue(removed["deleted"])
                self.assertEqual([row["id"] for row in store.list_tasks()], [tasks[1]["id"]])
                self.assertEqual(store.summary()["tasks_failed"], 0)

                self.assertEqual(store.clear_failed_tasks(), {"deleted": 0, "task_ids": []})

    def test_failure_releases_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"), patch.object(store, "_PROXIES", root / "proxies.json"):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                task = store.reserve_batch()[0]
                store.finish_failure(task["id"], "test failure")
                self.assertEqual(store.list_replacements()[0]["status"], "available")
                self.assertEqual(store.list_accounts()[0]["status"], "failed")

                retry = store.reserve_failed_account_retry(store.list_accounts()[0]["id"])
                self.assertEqual(retry["reason"], "reserved")
                self.assertEqual(retry["task"]["attempt"], 2)
                self.assertEqual(retry["task"]["retry_of_task_id"], task["id"])
                self.assertEqual(store.list_accounts()[0]["status"], "queued")
                self.assertEqual(store.list_replacements()[0]["status"], "reserved")

    def test_bad_replacement_is_quarantined_and_next_email_is_reserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"), patch.object(store, "_PROXIES", root / "proxies.json"):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("bad@example.com----https://mail.example/bad\ngood@example.com----https://mail.example/good")
                first = store.reserve_batch()[0]

                self.assertTrue(store.finish_replacement_failure(first["id"], "取码超时", "otp_unavailable"))
                replacements = store.list_replacements()
                self.assertEqual(replacements[0]["status"], "failed")
                self.assertEqual(replacements[0]["failure_code"], "otp_unavailable")
                self.assertEqual(store.summary()["replacement_failed"], 1)

                # 重复导入只更新 API，不得清掉失败标记。
                store.import_replacement_emails("bad@example.com----https://mail.example/new-api")
                self.assertEqual(store.list_replacements()[0]["status"], "failed")

                retry = store.reserve_retry(first["id"])
                self.assertEqual(retry["new_email"], "good@example.com")
                self.assertEqual(retry["attempt"], 2)
                self.assertEqual(retry["retry_of_task_id"], first["id"])
                self.assertEqual(store.list_accounts()[0]["status"], "queued")

                restored = store.restore_replacement(replacements[0]["id"])
                self.assertEqual(restored["status"], "available")
                self.assertNotIn("failure_reason", restored)

    def test_restart_after_committed_boundary_freezes_uncertain_identity(self):
        for stage in ("submit_new_email_otp", "changed", "relogin_new", "verified", "kept_open"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"), patch.object(store, "_PROXIES", root / "proxies.json"):
                    store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                    store.import_replacement_emails("maybe@example.com----https://mail.example/maybe")
                    task = store.reserve_batch()[0]
                    store.update_task(task["id"], status="running", stage=stage, message="换绑已进入提交边界")
                    self.assertEqual(store.recover_interrupted_tasks(), 1)
                    self.assertEqual(store.list_accounts()[0]["status"], "review")
                    self.assertEqual(store.list_replacements()[0]["status"], "review")
                    self.assertEqual(store.list_tasks()[0]["stage"], "manual_review")


if __name__ == "__main__":
    unittest.main()
