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
                patch.object(store, "_PROXIES", root / "proxies.json"),
                patch("app.worker.submit_tasks", return_value=1),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                client = app.create_app(recover=False).test_client()
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.post("/api/accounts/import", json={"text": "old@example.com----Password!----JBSWY3DPEHPK3PXP"}).status_code, 200)
                self.assertEqual(client.post("/api/replacements/import", json={"text": "new@example.com----https://mail.example/code"}).status_code, 200)
                self.assertEqual(client.post("/api/proxies/import", json={"text": "http://user:pass@proxy.example:8080"}).status_code, 200)
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
        self.assertIn("双重自动轮换", html)
        self.assertIn("失败原因", html)
        self.assertIn("换绑代理池", html)
        script = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn("data-restore-replacement", script)
        self.assertIn("replacement_failed", script)
        self.assertIn("data-restore-proxy", script)
        self.assertIn("proxy_available", script)
        self.assertIn("data-refresh-at", script)
        self.assertIn("data-close-roxy", script)
        self.assertIn("成功窗口规则", html)

    def test_success_account_can_refresh_at_and_close_roxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                task = store.reserve_batch()[0]
                store.finish_success(task["id"], {
                    "email": "new@example.com", "access_token": "at-new",
                    "roxy_profile_id": "profile-1", "roxy_browser_status": "open",
                })
                account_id = store.list_accounts()[0]["id"]
                client = app.create_app(recover=False).test_client()

                with patch("app.worker.submit_access_token_refresh", return_value={
                    "accepted": True, "account_id": account_id,
                }):
                    refresh = client.post(f"/api/accounts/{account_id}/refresh-at")
                self.assertEqual(refresh.status_code, 202)
                self.assertTrue(refresh.get_json()["accepted"])

                with patch("app.roxy_flow.close_retained_profile", return_value=True) as close:
                    response = client.post(f"/api/accounts/{account_id}/close-roxy")
                self.assertEqual(response.status_code, 200)
                close.assert_called_once_with("profile-1")
                self.assertEqual(response.get_json()["item"]["roxy_browser_status"], "closed")


if __name__ == "__main__":
    unittest.main()
