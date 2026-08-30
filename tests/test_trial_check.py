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
        assert kwargs["max_attempts"] == 1
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
            "has_active_plus_subscription": True, "current_plan_type": "plus",
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
    assert account["plus_active"] is True
    assert account["plus_plan"] == "plus"


def test_trial_check_rotates_static_proxy_pool(tmp_path):
    with (
        patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"),
        patch.object(store, "_REPLACEMENTS", tmp_path / "替换邮箱.json"),
        patch.object(store, "_TASKS", tmp_path / "rebind_tasks.json"),
        patch.object(store, "_PROXIES", tmp_path / "换绑代理.json"),
        patch.object(store, "_DETECTION_PROXIES", tmp_path / "资格检测代理.json"),
    ):
        store.import_source_accounts(
            "old1@example.com----Password!----JBSWY3DPEHPK3PXP\n"
            "old2@example.com----Password!----JBSWY3DPEHPK3PXP"
        )
        store.import_replacement_emails(
            "new1@example.com----https://mail.example/1\n"
            "new2@example.com----https://mail.example/2"
        )
        store.import_detection_proxies(
            "ID|socks5h://one.example:3000\n"
            "ID|socks5h://two.example:3000"
        )
        first, second = store.reserve_batch()
        store.finish_success(first["id"], {"email": "new1@example.com", "access_token": "at-1"})
        store.finish_success(second["id"], {"email": "new2@example.com", "access_token": "at-2"})

        claim1 = store.begin_trial_check(first["id"])
        claim2 = store.begin_trial_check(second["id"])

    assert claim1["proxy"]["proxy_url"] != claim2["proxy"]["proxy_url"]


def test_worker_retries_another_proxy_after_no_trial_result(tmp_path):
    with (
        patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"),
        patch.object(store, "_REPLACEMENTS", tmp_path / "替换邮箱.json"),
        patch.object(store, "_TASKS", tmp_path / "rebind_tasks.json"),
        patch.object(store, "_PROXIES", tmp_path / "换绑代理.json"),
        patch.object(store, "_DETECTION_PROXIES", tmp_path / "资格检测代理.json"),
        patch.object(worker.trial_check, "check_zero_trial", side_effect=[
            {"ok": True, "trial_zero_trial_eligible": False, "plus_trial_offer_kind": "none"},
            {"ok": True, "trial_zero_trial_eligible": True, "plus_trial_offer_kind": "free_trial"},
        ]) as check,
    ):
        store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
        store.import_replacement_emails("new@example.com----https://mail.example/code")
        store.import_detection_proxies(
            "ID|socks5h://one.example:3000\nID|socks5h://two.example:3000"
        )
        task = store.reserve_batch()[0]
        store.finish_success(task["id"], {"email": "new@example.com", "access_token": "at-new"})
        worker.submit_trial_check(1)

        import time
        for _ in range(50):
            account = store.get_success_account_context(1)
            if account and account.get("trial_zero_trial_eligible") is True:
                break
            time.sleep(0.02)

    assert check.call_count == 2
    assert account["trial_zero_trial_eligible"] is True


def test_successful_trial_proxy_is_preferred_for_next_account(tmp_path):
    with (
        patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"),
        patch.object(store, "_DETECTION_PROXIES", tmp_path / "资格检测代理.json"),
    ):
        store._write(store._ACCOUNTS, [{"id": 1, "status": "success", "access_token": "at"}])
        store.import_detection_proxies(
            "ID|socks5h://one.example:3000\nID|socks5h://two.example:3000"
        )
        rows = store._read(store._DETECTION_PROXIES)
        store.mark_detection_proxy_result(rows[1]["id"], {
            "ok": True,
            "checked_at": "2026-08-29T12:00:00",
            "trial_zero_trial_eligible": True,
        })
        claim = store.begin_trial_check(1)
        public_rows = store.list_detection_proxies()

    assert claim["proxy"]["id"] == rows[1]["id"]
    assert public_rows[1]["trial_eligible"] is True


def test_trial_proxy_retry_limit_is_five():
    assert worker._TRIAL_PROXY_RETRIES == 5


def test_worker_switches_proxy_after_transient_failure(tmp_path):
    with (
        patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"),
        patch.object(store, "_REPLACEMENTS", tmp_path / "替换邮箱.json"),
        patch.object(store, "_TASKS", tmp_path / "rebind_tasks.json"),
        patch.object(store, "_PROXIES", tmp_path / "换绑代理.json"),
        patch.object(store, "_DETECTION_PROXIES", tmp_path / "资格检测代理.json"),
        patch.object(worker.trial_check, "check_zero_trial", side_effect=[
            {"ok": False, "error": "proxy timeout", "retryable": True},
            {"ok": True, "trial_zero_trial_eligible": True, "plus_trial_offer_kind": "free_trial"},
        ]) as check,
    ):
        store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
        store.import_replacement_emails("new@example.com----https://mail.example/code")
        store.import_detection_proxies(
            "ID|socks5h://one.example:3000\nID|socks5h://two.example:3000"
        )
        task = store.reserve_batch()[0]
        store.finish_success(task["id"], {"email": "new@example.com", "access_token": "at-new"})
        worker.submit_trial_check(1)

        import time
        for _ in range(50):
            account = store.get_success_account_context(1)
            if account and account.get("trial_zero_trial_eligible") is True:
                break
            time.sleep(0.02)

    assert check.call_count == 2
    assert account["trial_zero_trial_eligible"] is True


def test_recover_interrupted_trial_check(tmp_path):
    with patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"):
        store._write(store._ACCOUNTS, [{
            "id": 1,
            "status": "success",
            "trial_check_status": "running",
            "trial_zero_trial_eligible": None,
        }])

        recovered = store.recover_interrupted_trial_checks()
        account = store.get_success_account_context(1)

    assert recovered == 1
    assert account["trial_check_status"] == "failed"
    assert "重新检测" in account["trial_check_error"]


def test_exhausted_proxy_retries_finish_with_last_result(tmp_path):
    with (
        patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"),
        patch.object(store, "_REPLACEMENTS", tmp_path / "替换邮箱.json"),
        patch.object(store, "_TASKS", tmp_path / "rebind_tasks.json"),
        patch.object(store, "_PROXIES", tmp_path / "换绑代理.json"),
        patch.object(store, "_DETECTION_PROXIES", tmp_path / "资格检测代理.json"),
        patch.object(worker, "_TRIAL_PROXY_RETRIES", 2),
        patch.object(worker.trial_check, "check_zero_trial", return_value={
            "ok": True, "checked_at": "2026-08-29T12:00:00",
            "trial_zero_trial_eligible": False, "plus_trial_offer_kind": "none",
        }),
    ):
        store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
        store.import_replacement_emails("new@example.com----https://mail.example/code")
        store.import_detection_proxies(
            "ID|socks5h://one.example:3000\nID|socks5h://two.example:3000"
        )
        task = store.reserve_batch()[0]
        store.finish_success(task["id"], {"email": "new@example.com", "access_token": "at-new"})
        worker.submit_trial_check(1)

        import time
        for _ in range(50):
            account = store.get_success_account_context(1)
            if account and account.get("trial_check_status") == "success":
                break
            time.sleep(0.02)

    assert account["trial_check_status"] == "success"
    assert account["trial_zero_trial_eligible"] is False


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


def test_detection_proxy_delete_route_removes_proxy_and_blocks_active_check(tmp_path):
    with (
        patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"),
        patch.object(store, "_DETECTION_PROXIES", tmp_path / "资格检测代理.json"),
    ):
        store.import_detection_proxies("ID|socks5h://proxy.example:3000")
        client = app.create_app(recover=False).test_client()
        proxy_id = store.list_detection_proxies()[0]["id"]

        deleted = client.delete(f"/api/detection-proxies/{proxy_id}")
        assert deleted.status_code == 200
        assert deleted.get_json()["deleted"] is True
        assert store.list_detection_proxies() == []
        assert client.delete(f"/api/detection-proxies/{proxy_id}").status_code == 404

        store.import_detection_proxies("ID|socks5h://proxy.example:3000")
        proxy_id = store.list_detection_proxies()[0]["id"]
        store._write(store._ACCOUNTS, [{
            "id": 7, "status": "success", "trial_check_status": "running",
            "trial_check_proxy_id": proxy_id,
        }])
        blocked = client.delete(f"/api/detection-proxies/{proxy_id}")

    assert blocked.status_code == 409
    assert blocked.get_json()["reason"] == "active_check"


def test_delete_all_detection_proxies_keeps_active_and_removes_idle(tmp_path):
    with (
        patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"),
        patch.object(store, "_DETECTION_PROXIES", tmp_path / "资格检测代理.json"),
    ):
        store.import_detection_proxies("ID|socks5h://one.example:3000\nUS|socks5h://two.example:3000")
        rows = store.list_detection_proxies()
        store._write(store._ACCOUNTS, [{
            "id": 8, "status": "success", "trial_check_status": "running",
            "trial_check_proxy_id": rows[0]["id"],
        }])
        result = store.delete_all_detection_proxies()
        remaining = store.list_detection_proxies()

    assert result["deleted"] == 1
    assert result["skipped_count"] == 1
    assert remaining[0]["id"] == rows[0]["id"]
