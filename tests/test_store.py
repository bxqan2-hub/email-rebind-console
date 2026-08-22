# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import store


class StoreTests(unittest.TestCase):
    def test_main_format_pair_and_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"):
                imported = store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                pool = store.import_replacement_emails("new@example.com----https://mail.example/code")
                self.assertEqual(imported["inserted"], 1)
                self.assertEqual(pool["inserted"], 1)
                tasks = store.reserve_batch()
                self.assertEqual(tasks[0]["old_email"], "old@example.com")
                self.assertEqual(tasks[0]["new_email"], "new@example.com")
                store.finish_success(tasks[0]["id"], {"email": "new@example.com", "access_token": "at-new"})
                self.assertEqual(
                    store.export_success_lines(),
                    ["new@example.com----Password!----JBSWY3DPEHPK3PXP----at-new"],
                )

    def test_failure_releases_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                task = store.reserve_batch()[0]
                store.finish_failure(task["id"], "test failure")
                self.assertEqual(store.list_replacements()[0]["status"], "available")
                self.assertEqual(store.list_accounts()[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()

