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

    def test_failed_replacement_can_be_restored_by_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), patch.object(store, "_REPLACEMENTS", root / "replacements.json"), patch.object(store, "_TASKS", root / "tasks.json"):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("bad@example.com----https://mail.example/bad")
                task = store.reserve_batch()[0]
                store.finish_replacement_failure(task["id"], "没有验证码", "otp_unavailable")
                client = app.create_app(recover=False).test_client()
                response = client.post(f"/api/replacements/{task['replacement_id']}/restore")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["item"]["status"], "available")

    def test_page_explains_automatic_rotation_and_failed_pool_controls(self):
        client = app.create_app(recover=False).test_client()
        html = client.get("/").get_data(as_text=True)
        self.assertIn("自动轮换规则", html)
        self.assertIn("失败原因", html)
        script = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn("data-restore-replacement", script)
        self.assertIn("replacement_failed", script)


if __name__ == "__main__":
    unittest.main()
