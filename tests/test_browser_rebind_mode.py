import json
from pathlib import Path
from unittest.mock import patch

import app
import store
import worker


def test_browser_mode_is_persisted_and_dispatched_to_historical_flow(tmp_path):
    with (
        patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"),
        patch.object(store, "_REPLACEMENTS", tmp_path / "replacements.json"),
        patch.object(store, "_TASKS", tmp_path / "tasks.json"),
        patch.object(store, "_PROXIES", tmp_path / "proxies.json"),
        patch.object(worker, "submit_tasks", return_value=1) as submit,
    ):
        store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
        store.import_replacement_emails("new@example.com----https://mail.example/code")
        store.import_proxies("http://proxy.example:8080")
        client = app.create_app(recover=False).test_client()
        response = client.post(
            "/api/rebind/start",
            json={"account_ids": [1], "workers": 1, "rebind_mode": "browser"},
        )

        assert response.status_code == 200
        task = store.list_tasks()[0]
        assert task["rebind_mode"] == "browser"
        assert task["open_roxy_after"] is False
        submit.assert_called_once()


def test_historical_full_roxy_module_contains_complete_flow():
    source = Path("roxy_browser_rebind.py").read_text(encoding="utf-8")
    assert "def perform_email_rebind(" in source
    assert "_change_email_har_guided(" in source
    assert "client.open_profile" in source
    assert "_retain_browser(" in source
    assert "if code_input and password_totp_login and not totp_submitted" in source


def test_worker_dispatches_browser_mode_to_restored_flow(tmp_path):
    with (
        patch.object(store, "_ACCOUNTS", tmp_path / "accounts.json"),
        patch.object(store, "_REPLACEMENTS", tmp_path / "replacements.json"),
        patch.object(store, "_TASKS", tmp_path / "tasks.json"),
        patch.object(store, "_PROXIES", tmp_path / "proxies.json"),
        patch.object(worker.browser_rebind, "perform_email_rebind", return_value={
            "email": "new@example.com", "access_token": "at-new",
        }) as browser,
        patch.object(worker.protocol_flow, "run_upstream_rebind") as protocol,
    ):
        store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
        store.import_replacement_emails("new@example.com----https://mail.example/code")
        store.import_proxies("http://proxy.example:8080")
        task = store.reserve_batch(rebind_mode="browser")[0]
        worker._run(task["id"])
        assert store.list_accounts()[0]["status"] == "success"

    browser.assert_called_once()
    assert browser.call_args.kwargs["auth_method"] == "password_totp"
    protocol.assert_not_called()
