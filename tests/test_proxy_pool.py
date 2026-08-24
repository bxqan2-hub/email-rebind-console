# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
import roxy_flow
import store
import worker


class ProxyPoolTests(unittest.TestCase):
    def test_import_masks_credentials_quarantines_and_restores(self):
        with tempfile.TemporaryDirectory() as tmp:
            proxy_file = Path(tmp) / "proxies.json"
            fake_random = Mock()
            fake_random.choice.side_effect = lambda rows: rows[0]
            with patch.object(store, "_PROXIES", proxy_file), patch.object(store, "_PROXY_RANDOM", fake_random):
                result = store.import_proxies(
                    "http://alice:secret@one.example:8000\n"
                    "two.example:9000:bob:password\n"
                    "three.example:7000----carol----token\n"
                    "not-a-proxy"
                )
                self.assertEqual(result["parsed"], 3)
                self.assertEqual(len(result["invalid"]), 1)
                public = store.list_proxies()
                self.assertEqual(len(public), 3)
                self.assertNotIn("proxy_url", public[0])
                self.assertEqual(public[0]["display"], "http://***:***@one.example:8000")
                self.assertNotIn("secret", str(public))

                first = store.pick_random_proxy()
                self.assertEqual(first["id"], public[0]["id"])
                self.assertTrue(store.mark_proxy_failure(
                    first["id"], task_id=11, old_email="old@example.com", error="出口检测失败",
                ))
                # 重复导入只更新记录，不得悄悄恢复失败代理。
                store.import_proxies("http://alice:secret@one.example:8000")
                self.assertEqual(store.list_proxies()[0]["status"], "failed")
                second = store.pick_random_proxy()
                self.assertNotEqual(second["id"], first["id"])
                restored = store.restore_proxy(first["id"])
                self.assertEqual(restored["status"], "available")
                self.assertNotIn("failure_reason", restored)

    def test_worker_proxy_failure_switches_proxy_without_consuming_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_random = Mock()
            fake_random.choice.side_effect = lambda rows: rows[0]
            seen: list[str] = []

            def perform(**kwargs):
                seen.append(kwargs["proxy_url"])
                if len(seen) == 1:
                    raise roxy_flow.ProxyFailure("Roxy 代理出口快速检测失败")
                return {"email": kwargs["new_email"], "access_token": "at-new"}

            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(store, "_PROXY_RANDOM", fake_random), \
                    patch.object(worker.settings, "MAX_PROXY_ATTEMPTS", 5), \
                    patch.object(worker.protocol_flow, "perform_email_rebind", side_effect=perform):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://first.example:8000\nhttp://second.example:8000")
                task = store.reserve_batch()[0]
                worker._run(task["id"])

                self.assertEqual(len(seen), 2)
                self.assertNotEqual(seen[0], seen[1])
                proxies = store.list_proxies()
                self.assertEqual(proxies[0]["status"], "failed")
                self.assertIn("代理出口", proxies[0]["failure_reason"])
                self.assertEqual(proxies[1]["status"], "available")
                self.assertEqual(proxies[1]["success_count"], 1)
                self.assertEqual(store.list_replacements()[0]["status"], "used")
                self.assertEqual(store.list_accounts()[0]["status"], "success")
                final_task = store.list_tasks()[0]
                self.assertEqual(final_task["proxy_attempt"], 2)
                self.assertEqual(final_task["proxy_id"], proxies[1]["id"])

    def test_proxy_pool_exhaustion_releases_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"), \
                    patch.object(worker.settings, "MAX_PROXY_ATTEMPTS", 5), \
                    patch.object(worker.protocol_flow, "perform_email_rebind", side_effect=roxy_flow.ProxyFailure("代理超时")):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                store.import_proxies("http://only.example:8000")
                task = store.reserve_batch()[0]
                worker._run(task["id"])

                self.assertEqual(store.list_proxies()[0]["status"], "failed")
                self.assertEqual(store.list_replacements()[0]["status"], "available")
                self.assertEqual(store.list_accounts()[0]["status"], "failed")
                self.assertIn("没有可用代理", store.list_accounts()[0]["error"])

    def test_start_route_requires_manual_proxy_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(store, "_ACCOUNTS", root / "accounts.json"), \
                    patch.object(store, "_REPLACEMENTS", root / "replacements.json"), \
                    patch.object(store, "_TASKS", root / "tasks.json"), \
                    patch.object(store, "_PROXIES", root / "proxies.json"):
                store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
                store.import_replacement_emails("new@example.com----https://mail.example/code")
                client = app.create_app(recover=False).test_client()
                response = client.post("/api/rebind/start", json={"account_ids": [], "workers": 1})
                self.assertEqual(response.status_code, 409)
                self.assertIn("请先手动导入", response.get_json()["error"])

    def test_roxy_preflight_failure_becomes_proxy_failure(self):
        class FakeClient:
            def __init__(self, profile_proxy):
                self.profile_proxy = profile_proxy

            def open_profile(self, **kwargs):
                self.kwargs = kwargs
                raise RuntimeError("Roxy 代理出口快速检测失败（已检测 1 条）")

        with patch.object(roxy_flow, "_load_main_roxy", return_value=(FakeClient, None, None, None, None, None, None)):
            with self.assertRaises(roxy_flow.ProxyFailure):
                roxy_flow.perform_email_rebind(
                    old_email="old@example.com", new_email="new@example.com",
                    password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                    api_url="https://mail.example/code", proxy_url="http://proxy.example:8000",
                )

    def test_roxy_window_exit_failure_is_cleaned_before_login(self):
        events: list[str] = []

        class FakeDriver:
            def set_page_load_timeout(self, _value):
                pass

            def set_script_timeout(self, _value):
                pass

            def quit(self):
                events.append("quit")

        class FakeClient:
            def __init__(self, profile_proxy):
                self.profile_proxy = profile_proxy

            def open_profile(self, **kwargs):
                events.append(f"open:{kwargs.get('require_proxy_exit_ip')}")
                return SimpleNamespace(profile_id="p1", preflight_exit_geo={"ip": "203.0.113.10"})

            def cleanup_profile(self, _opened):
                events.append("cleanup")

            def close_profile(self, profile_id):
                events.append(f"close:{profile_id}")
                return True

            def delete_profile(self, profile_id):
                events.append(f"delete:{profile_id}")
                return True

        loaded = (FakeClient, lambda _opened: FakeDriver(), lambda _driver: None, None, None, None, lambda *_a, **_k: {})
        with patch.object(roxy_flow, "_load_main_roxy", return_value=loaded):
            with self.assertRaises(roxy_flow.ProxyFailure):
                roxy_flow.perform_email_rebind(
                    old_email="old@example.com", new_email="new@example.com",
                    password="Password!", totp_secret="JBSWY3DPEHPK3PXP",
                    api_url="https://mail.example/code", proxy_url="http://proxy.example:8000",
                )
        self.assertEqual(events, ["open:True", "quit", "close:p1", "delete:p1"])


if __name__ == "__main__":
    unittest.main()
