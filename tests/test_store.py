# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store


class StoreTests(unittest.TestCase):
    def test_smart_import_routes_account_and_email_url_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"):
                result = store.import_smart_entries(
                    "old@example.com----Password!----JBSWY3DPEHPK3PXP\n"
                    "new@example.com----https://mail.example/code\n"
                    "bad input"
                )

                self.assertEqual(result["parsed"], 2)
                self.assertEqual(result["account_parsed"], 1)
                self.assertEqual(result["replacement_parsed"], 1)
                self.assertEqual(result["inserted"], 2)
                self.assertEqual(result["invalid"], [{
                    "line": 3,
                    "reason": "需要：邮箱----密码----MFA Secret，或 邮箱----http(s)://API取码地址",
                }])
                self.assertEqual(store.list_accounts()[0]["old_email"], "old@example.com")
                self.assertEqual(store.list_replacements()[0]["email"], "new@example.com")
                self.assertTrue(store.list_replacements()[0]["has_api"])

    def test_smart_import_accepts_email_url_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"):
                result = store.import_smart_entries(
                    "mail@example.com====https://mail.example/api?email={email}"
                )

                self.assertEqual(result["account_parsed"], 0)
                self.assertEqual(result["replacement_parsed"], 1)
                self.assertEqual(store.list_accounts(), [])
                self.assertEqual(store.list_replacements()[0]["email"], "mail@example.com")

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
                    ["new@example.com----Password!----JBSWY3DPEHPK3PXP----at-new"],
                )
                account = store.list_accounts()[0]
                self.assertEqual(account["roxy_browser_status"], "open")
                self.assertEqual(account["roxy_profile_id"], "profile-1")
                self.assertEqual(store.summary()["roxy_open"], 1)

                started = store.begin_access_token_refresh(account["id"])
                self.assertEqual(started["at_refresh_status"], "running")
                refreshed = store.finish_access_token_refresh(account["id"], {"access_token": "at-plus"})
                self.assertEqual(refreshed["at_refresh_status"], "success")
                self.assertEqual(
                    store.export_success_lines(),
                    ["new@example.com----Password!----JBSWY3DPEHPK3PXP----at-plus"],
                )
                closed = store.mark_roxy_profile_closed(account["id"])
                self.assertEqual(closed["roxy_browser_status"], "closed")
                self.assertEqual(store.summary()["roxy_open"], 0)

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

    def test_restart_after_otp_submission_freezes_uncertain_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"), patch.object(store, "_PROXIES", root / "proxies.json"):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("maybe@example.com----https://mail.example/maybe")
                task = store.reserve_batch()[0]
                store.update_task(task["id"], status="running", stage="submit_new_email_otp", message="验证码已提交")
                self.assertEqual(store.recover_interrupted_tasks(), 1)
                self.assertEqual(store.list_accounts()[0]["status"], "review")
                self.assertEqual(store.list_replacements()[0]["status"], "review")
                self.assertEqual(store.list_tasks()[0]["stage"], "manual_review")


if __name__ == "__main__":
    unittest.main()
