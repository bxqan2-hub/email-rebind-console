# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import store


class AppTests(unittest.TestCase):
    def test_account_import_route_keeps_email_url_out_of_replacement_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"):
                client = app.create_app(recover=False).test_client()
                response = client.post("/api/accounts/import", json={
                    "text": "new@example.com----https://mail.example/code",
                })

                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual(body["parsed"], 1)
                self.assertEqual(store.list_accounts()[0]["old_email"], "new@example.com")
                self.assertTrue(store.list_accounts()[0]["has_api"])
                self.assertEqual(store.list_replacements(), [])

    def test_replacement_and_proxy_delete_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"):
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://proxy.example:8080")
                replacement_id = store.list_replacements()[0]["id"]
                proxy_id = store.list_proxies()[0]["id"]
                client = app.create_app(recover=False).test_client()

                self.assertEqual(client.delete(f"/api/replacements/{replacement_id}").status_code, 200)
                self.assertEqual(client.delete(f"/api/proxies/{proxy_id}").status_code, 200)
                self.assertEqual(client.delete(f"/api/replacements/{replacement_id}").status_code, 404)
                self.assertEqual(client.delete(f"/api/proxies/{proxy_id}").status_code, 404)

    def test_original_account_delete_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"):
                store.import_source_accounts("old@example.com----https://mail.example/old")
                account_id = store.list_accounts()[0]["id"]
                client = app.create_app(recover=False).test_client()

                self.assertEqual(client.delete(f"/api/accounts/{account_id}").status_code, 200)
                self.assertEqual(store.list_accounts(), [])
                self.assertEqual(client.delete(f"/api/accounts/{account_id}").status_code, 404)

    def test_active_task_pool_items_cannot_be_deleted_by_route(self):
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
                client = app.create_app(recover=False).test_client()

                replacement = client.delete(f"/api/replacements/{task['replacement_id']}")
                proxy_response = client.delete(f"/api/proxies/{proxy['id']}")
                self.assertEqual(replacement.status_code, 409)
                self.assertEqual(proxy_response.status_code, 409)
                self.assertEqual(replacement.get_json()["task_id"], task["id"])
                self.assertEqual(proxy_response.get_json()["task_id"], task["id"])

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
                task = store.list_tasks()[0]
                store.finish_success(task["id"], {
                    "email": "new@example.com", "access_token": "at-new",
                    "roxy_profile_id": "profile-1",
                })
                account_id = store.list_accounts()[0]["id"]
                exported = client.get("/api/export")
                self.assertEqual(exported.status_code, 200)
                self.assertEqual(
                    exported.get_data(as_text=True).strip(),
                    "old@example.com----new@example.com----Password!----JBSWY3DPEHPK3PXP----at-new",
                )
                self.assertIn("filename*=UTF-8''%E6%8D%A2%E7%BB%91%E5%AE%8C%E6%88%90-", exported.headers["Content-Disposition"])
                one = client.get(f"/api/accounts/{account_id}/export")
                self.assertEqual(one.status_code, 200)
                self.assertEqual(one.get_data(as_text=True), exported.get_data(as_text=True))
                at = client.get(f"/api/accounts/{account_id}/access-token")
                self.assertEqual(at.status_code, 200)
                self.assertEqual(at.get_data(as_text=True), "at-new")
                self.assertEqual(at.headers["Cache-Control"], "no-store")
                self.assertIsNotNone(store.begin_access_token_refresh(account_id))
                saved = store.finish_access_token_refresh(account_id, {"access_token": "at-saved"})
                self.assertTrue(saved["at_saved"])
                self.assertNotIn("access_token", saved)
                refreshed_export = client.get(f"/api/accounts/{account_id}/export")
                self.assertIn("----at-saved\n", refreshed_export.get_data(as_text=True))
                self.assertEqual(
                    client.get(f"/api/accounts/{account_id}/access-token").get_data(as_text=True),
                    "at-saved",
                )
                public = client.get("/api/state").get_json()["accounts"][0]
                self.assertTrue(public["at_saved"])
                self.assertTrue(public["at_saved_at"])
                removed_task = client.delete(f"/api/tasks/{task['id']}")
                self.assertEqual(removed_task.status_code, 200)
                self.assertEqual(client.get(f"/api/accounts/{account_id}/export").status_code, 200)

    def test_failed_task_log_cleanup_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                task = store.reserve_batch()[0]
                store.finish_failure(task["id"], "test failure")
                client = app.create_app(recover=False).test_client()

                self.assertEqual(client.delete(f"/api/tasks/{task['id']}").status_code, 200)
                self.assertEqual(client.delete(f"/api/tasks/{task['id']}").status_code, 404)
                self.assertEqual(client.delete("/api/tasks/failed").get_json()["deleted"], 0)

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
        self.assertIn("waitForAccessTokenRefresh", script)
        self.assertIn("AT 已更新", script)
        self.assertIn("AT 未变化", script)
        self.assertIn("已保存，可复制", script)
        self.assertIn("AT 已更新并保存，可直接复制", script)
        self.assertIn("data-close-roxy", script)
        self.assertIn("data-copy-export", script)
        self.assertIn("data-copy-at", script)
        self.assertNotIn("data-download-export", script)
        self.assertIn("data-delete-finished-task", script)
        self.assertIn("data-delete-success-account", script)
        self.assertIn("clearFinishedTasks", script)
        self.assertIn("清理全部已结束", html)
        self.assertNotIn("downloadExport", script)
        self.assertIn("document.execCommand('copy')", script)
        self.assertIn("原邮箱----换绑后邮箱----密码----2FA----AT", html)
        self.assertIn("原邮箱----换绑后邮箱----替换邮箱URL----AT", html)
        self.assertIn("复制AT", html)
        self.assertNotIn("id=\"downloadExport\"", html)
        self.assertIn("界面自动刷新", html)
        self.assertIn("成功窗口规则", html)
        self.assertIn("不会再进入替换邮箱号池", html)
        self.assertIn("和原邮箱入口完全分开", html)
        self.assertNotIn("智能导入", html)
        self.assertNotIn("account_parsed", script)
        self.assertIn("导入原邮箱账号", html)
        self.assertIn("data-delete-replacement", script)
        self.assertIn("data-delete-proxy", script)
        self.assertIn("data-delete-account", script)
        self.assertIn("data-retry-account", script)
        self.assertIn("自动重试中", script)
        self.assertIn("失败重试", script)
        self.assertIn("btn danger", script)

    def test_failed_account_retry_route_submits_traced_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch("app.worker.submit_tasks", return_value=1) as submit:
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://proxy.example:8080")
                first = store.reserve_batch()[0]
                store.finish_failure(first["id"], "test failure")
                account_id = store.list_accounts()[0]["id"]
                client = app.create_app(recover=False).test_client()

                response = client.post(f"/api/accounts/{account_id}/retry")

                self.assertEqual(response.status_code, 202)
                task = response.get_json()["task"]
                self.assertEqual(task["attempt"], 2)
                self.assertEqual(task["retry_of_task_id"], first["id"])
                submit.assert_called_once_with([task], 1)

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
                self.assertEqual(client.delete(f"/api/accounts/{account_id}").status_code, 409)

                with patch("app.roxy_flow.delete_retained_profile", return_value=True) as delete_profile:
                    response = client.post(f"/api/accounts/{account_id}/close-roxy")
                self.assertEqual(response.status_code, 200)
                delete_profile.assert_called_once_with("profile-1")
                self.assertTrue(response.get_json()["deleted"])
                self.assertEqual(response.get_json()["item"]["roxy_browser_status"], "deleted")
                self.assertEqual(response.get_json()["item"]["roxy_profile_id"], "")
                repeated = client.post(f"/api/accounts/{account_id}/close-roxy")
                self.assertTrue(repeated.get_json()["already_deleted"])
                cleaned = client.delete(f"/api/accounts/{account_id}")
                self.assertEqual(cleaned.status_code, 200)
                self.assertEqual(store.list_accounts(), [])

    def test_bulk_cleanup_removes_success_and_failed_logs_but_keeps_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"):
                store.import_source_accounts(
                    "first@example.com----Password!----JBSWY3DPEHPK3PXP\n"
                    "second@example.com----Password!----JBSWY3DPEHPK3PXP"
                )
                store.import_replacement_emails(
                    "new1@example.com----https://mail.example/1\n"
                    "new2@example.com----https://mail.example/2"
                )
                tasks = store.reserve_batch()
                store.finish_failure(tasks[0]["id"], "failed")
                store.finish_success(tasks[1]["id"], {
                    "email": "new2@example.com", "access_token": "at-2",
                })
                client = app.create_app(recover=False).test_client()

                response = client.delete("/api/tasks/finished")

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["deleted"], 2)
                self.assertEqual(store.list_tasks(), [])
                self.assertEqual(len(store.export_success_lines()), 1)


if __name__ == "__main__":
    unittest.main()
