# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import roxy_flow
import store
import worker


class WorkerRotationTests(unittest.TestCase):
    def test_otp_failure_marks_email_and_automatically_uses_next_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = (
                patch.object(store, "_ACCOUNTS", root / "accounts.json"),
                patch.object(store, "_REPLACEMENTS", root / "replacements.json"),
                patch.object(store, "_TASKS", root / "tasks.json"),
            )
            with storage[0], storage[1], storage[2], patch.object(worker.settings, "MAX_REPLACEMENT_ATTEMPTS", 5), patch.object(
                worker.roxy_flow,
                "perform_email_rebind",
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
                initial = store.reserve_batch()[0]
                worker._run(initial["id"])

                self.assertEqual(perform.call_count, 2)
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

    def test_pool_exhaustion_keeps_account_failed_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"), patch.object(
                worker.roxy_flow,
                "perform_email_rebind",
                side_effect=roxy_flow.ReplacementEmailFailure("otp_unavailable", "没有验证码"),
            ):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("bad@example.com----https://mail.example/bad")
                initial = store.reserve_batch()[0]
                worker._run(initial["id"])
                account = store.list_accounts()[0]
                self.assertEqual(account["status"], "failed")
                self.assertIn("号池没有更多可用邮箱", account["error"])
                self.assertEqual(store.list_replacements()[0]["status"], "failed")

    def test_unknown_post_change_result_is_frozen_for_manual_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"), patch.object(
                worker.roxy_flow,
                "perform_email_rebind",
                side_effect=roxy_flow.RebindOutcomeUnknown("maybe@example.com", "验证码已提交但 AT 刷新失败"),
            ):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("maybe@example.com----https://mail.example/maybe")
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


if __name__ == "__main__":
    unittest.main()
