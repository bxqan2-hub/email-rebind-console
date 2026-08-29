from unittest.mock import patch

import store
import trial_check
import worker
import app


def test_detection_proxy_import_preserves_country_label_and_masks_credentials(tmp_path):
    with patch.object(store, "_DETECTION_PROXIES", tmp_path / "资格检测代理.json"):
        result = store.import_detection_proxies("ID|socks5h://user:secret@proxy.example:3000")
        rows = store.list_detection_proxies()

    assert result["parsed"] == 1
    assert rows[0]["country"] == "ID"
    assert rows[0]["display"] == "socks5h://***:***@proxy.example:3000"


def test_trial_check_classifies_zero_trial_and_country():
    class FakeDetectionProxy:
        @staticmethod
        def infer_detection_proxy_country(spec):
            return "ID"

        @staticmethod
        def resolve_static_detection_proxy(spec):
            return "socks5h://proxy.example:3000"

        @staticmethod
        def inspect_static_proxy(spec):
            raise AssertionError("labeled country should not require an extra geo request")

        @staticmethod
        def infer_timezone_offset_min(_spec, fallback="-"):
            return fallback

    def fake_plan(*args, **kwargs):
        assert kwargs["proxy"] == "socks5h://proxy.example:3000"
        return {"ok": True, "plus_trial_eligible": True, "plus_trial_offer_kind": "free_trial", "plus_trial_offer_label": "0元试用"}

    with patch.object(trial_check, "_load_main_modules", return_value=(FakeDetectionProxy, fake_plan)):
        result = trial_check.check_zero_trial("token", "ID|socks5h://proxy.example:3000")

    assert result["trial_zero_trial_eligible"] is True
    assert result["trial_proxy_country"] == "ID"


def test_worker_trial_check_persists_result(tmp_path):
    with (
        patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"),
        patch.object(store, "_REPLACEMENTS", tmp_path / "替换邮箱.json"),
        patch.object(store, "_TASKS", tmp_path / "tasks.json"),
        patch.object(store, "_PROXIES", tmp_path / "换绑代理.json"),
        patch.object(store, "_DETECTION_PROXIES", tmp_path / "资格检测代理.json"),
        patch.object(worker.trial_check, "check_zero_trial", return_value={
            "ok": True, "checked_at": "2026-08-29T12:00:00", "trial_zero_trial_eligible": True,
            "trial_proxy_country": "ID", "plus_trial_offer_kind": "free_trial",
        }),
    ):
        store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
        store.import_replacement_emails("new@example.com----https://mail.example/code")
        store.import_detection_proxies("ID|socks5h://proxy.example:3000")
        task = store.reserve_batch()[0]
        store.finish_success(task["id"], {"email": "new@example.com", "access_token": "at-new"})
        result = worker.submit_trial_check(1)

        # The worker pool is asynchronous; wait for the submitted check to leave running.
        import time
        for _ in range(50):
            account = store.get_success_account_context(1)
            if account and account.get("trial_check_status") == "success":
                break
            time.sleep(0.02)

        account = store.get_success_account_context(1)

    assert result["accepted"] is True
    assert account["trial_zero_trial_eligible"] is True
    assert account["trial_check_proxy_country"] == "ID"


def test_refreshing_at_resets_trial_state_for_followup_auto_check(tmp_path):
    with patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"):
        row = {
            "id": 1, "status": "success", "access_token": "old-at",
            "trial_check_status": "success", "trial_zero_trial_eligible": True,
        }
        store._write(store._ACCOUNTS, [row])
        store.finish_access_token_refresh(1, {"access_token": "new-at"})
        account = store.get_success_account_context(1)

    assert account["access_token"] == "new-at"
    assert account["trial_check_status"] == "not_configured"
    assert account["trial_zero_trial_eligible"] is None


def test_detection_proxy_import_route_and_state(tmp_path):
    with patch.object(store, "_DETECTION_PROXIES", tmp_path / "资格检测代理.json"):
        client = app.create_app(recover=False).test_client()
        response = client.post(
            "/api/detection-proxies/import",
            json={"text": "ID|socks5h://user:secret@proxy.example:3000"},
        )
        state = client.get("/api/state").get_json()

    assert response.status_code == 200
    assert state["detection_proxies"][0]["country"] == "ID"
    assert "secret" not in state["detection_proxies"][0]["display"]
