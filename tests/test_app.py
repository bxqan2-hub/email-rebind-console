# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import store


class AppTests(unittest.TestCase):
    def test_import_preview_start_and_export_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            patches = (
                patch.object(store, "_ACCOUNTS", root / "accounts.json"),
                patch.object(store, "_REPLACEMENTS", root / "replacements.json"),
                patch.object(store, "_TASKS", root / "tasks.json"),
                patch("app.worker.submit_tasks", return_value=1),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                client = app.create_app(recover=False).test_client()
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.post("/api/accounts/import", json={"text": "old@example.com----Password!----JBSWY3DPEHPK3PXP"}).status_code, 200)
                self.assertEqual(client.post("/api/replacements/import", json={"text": "new@example.com----https://mail.example/code"}).status_code, 200)
                preview = client.post("/api/pairs/preview", json={"account_ids": []}).get_json()
                self.assertEqual(preview["pairs"][0]["new_email"], "new@example.com")
                started = client.post("/api/rebind/start", json={"account_ids": [], "workers": 2})
                self.assertEqual(started.status_code, 200)
                self.assertEqual(started.get_json()["submitted"], 1)
                self.assertEqual(client.get("/api/export").status_code, 200)


if __name__ == "__main__":
    unittest.main()
