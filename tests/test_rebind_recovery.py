import json
from unittest.mock import Mock

import pytest

import app
import protocol_flow
import roxy_flow
import store
import worker


@pytest.fixture
def storage(tmp_path, monkeypatch):
    for name in ("_ACCOUNTS", "_TASKS", "_REPLACEMENTS", "_PROXIES", "_DETECTION_PROXIES"):
        monkeypatch.setattr(store, name, tmp_path / f"{name}.json")
    monkeypatch.setattr(worker, "submit_trial_check", Mock())
    store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
    store.import_replacement_emails("new@example.com----https://mail.example/new")
    store.import_proxies("http://proxy.example:8080")
    return store.reserve_batch()[0]


def confirmed_failure(task):
    store.update_task(task["id"], status="running", stage="changed", email_change_confirmed=True)
    store.finish_review_failure(task["id"], "new@example.com", "invalid_state")


def test_confirmed_change_survives_at_failure_and_deleting_both_records(storage, monkeypatch):
    confirmed_failure(storage)
    account = store.list_accounts()[0]
    assert account["status"] == "pending_at"
    assert account["email_change_confirmed"] is True
    assert account["email_change_uncertain"] is False
    assert account["has_replacement_api"] is True
    assert store.list_replacements()[0]["status"] == "used"
    assert store.delete_replacement(storage["replacement_id"])["deleted"]
    assert store.clear_finished_tasks()["deleted"] == 1
    login = Mock(return_value={"email": "new@example.com", "access_token": "new-at"})
    rebind, browser = Mock(), Mock()
    monkeypatch.setattr(protocol_flow, "refresh_access_token_protocol", login)
    monkeypatch.setattr(protocol_flow, "run_upstream_rebind", rebind)
    monkeypatch.setattr(roxy_flow, "perform_replacement_login", browser)
    retry = store.reserve_review_login_retry(1)["task"]
    assert retry["replacement_id"] == 0
    assert retry["rebind_mode"] == "protocol"
    worker._run(retry["id"])
    assert store.list_accounts()[0]["status"] == "success"
    assert store.get_success_access_token(1) == "new-at"
    assert store.list_replacements() == []
    login.assert_called_once()
    assert login.call_args.kwargs["email"] == "new@example.com"
    rebind.assert_not_called()
    browser.assert_not_called()


@pytest.mark.parametrize("retry_succeeds", [True, False])
@pytest.mark.parametrize("failure_kind", ["confirmed_error", "unexpected_error", "trace_only"])
def test_initial_at_failure_automatically_relogs_once_without_rebinding(storage, monkeypatch, retry_succeeds, failure_kind):
    def rebind(**kwargs):
        if failure_kind != "trace_only":
            kwargs["progress"]("changed", "server confirmed")
        if failure_kind == "unexpected_error":
            raise RuntimeError("login result could not be saved")
        raise roxy_flow.RebindOutcomeUnknown("new@example.com", "AT missing", confirmed=True)
    rebind_mock = Mock(side_effect=rebind)
    login = Mock(return_value={"email": "new@example.com", "access_token": "new-at"})
    if not retry_succeeds:
        login.side_effect = RuntimeError("invalid_state")
    monkeypatch.setattr(protocol_flow, "run_upstream_rebind", rebind_mock)
    monkeypatch.setattr(protocol_flow, "refresh_access_token_protocol", login)
    worker._run(storage["id"])
    rebind_mock.assert_called_once()
    login.assert_called_once()
    assert login.call_args.kwargs["email"] == "new@example.com"
    assert store.list_accounts()[0]["status"] == ("success" if retry_succeeds else "pending_at")
    assert any(x["stage"] == "protocol_at_retry" for x in store.list_tasks()[0]["stage_history"])
    assert store.list_replacements()[0]["status"] == "used"


def test_terminal_failure_does_not_release_a_confirmed_bound_email(storage):
    store.update_task(storage["id"], status="running", stage="changed")
    store.finish_failure(storage["id"], "login result could not be saved")
    assert store.list_accounts()[0]["status"] == "pending_at"
    assert store.list_accounts()[0]["current_email"] == "new@example.com"
    assert store.list_replacements()[0]["status"] == "used"


@pytest.mark.parametrize("bundle", [[], {"email": "old@example.com", "access_token": "old-at"}])
def test_invalid_bundle_after_confirmed_change_remains_recoverable(tmp_path, monkeypatch, bundle):
    path = tmp_path / "login_bundle.json"
    path.write_text(json.dumps(bundle), encoding="utf-8")
    monkeypatch.setattr(protocol_flow, "run_rebind_email", Mock(return_value=protocol_flow.RebindResult(
        ok=True, code="OK", message="rebind success", bundle_path=str(path),
    )))
    with pytest.raises(roxy_flow.RebindOutcomeUnknown) as error:
        protocol_flow.run_upstream_rebind(
            old_email="old@example.com", new_email="new@example.com", password="password",
            totp_secret="secret", api_url="https://mail.example/new", proxy_url="",
        )
    assert error.value.confirmed is True
    assert error.value.new_email == "new@example.com"


def test_uncertain_submit_does_not_claim_success_or_trigger_automatic_login(storage, monkeypatch):
    rebind = Mock(side_effect=roxy_flow.RebindOutcomeUnknown("new@example.com", "verify timeout"))
    login = Mock()
    monkeypatch.setattr(protocol_flow, "run_upstream_rebind", rebind)
    monkeypatch.setattr(protocol_flow, "refresh_access_token_protocol", login)
    worker._run(storage["id"])
    login.assert_not_called()
    assert store.list_accounts()[0]["status"] == "review"


def test_manual_retry_after_cleanup_retries_new_session_only(storage, monkeypatch):
    confirmed_failure(storage)
    store.delete_replacement(storage["replacement_id"])
    store.clear_finished_tasks()
    login = Mock(side_effect=[RuntimeError("invalid_state"), {"email": "new@example.com", "access_token": "new-at"}])
    monkeypatch.setattr(protocol_flow, "refresh_access_token_protocol", login)
    retry = store.reserve_review_login_retry(1, max_transient_retries=1)["task"]
    worker._run(retry["id"])
    assert login.call_count == 2
    assert all(call.kwargs["email"] == "new@example.com" for call in login.call_args_list)
    assert store.list_accounts()[0]["status"] == "success"


def test_legacy_review_with_no_logs_no_pool_can_start_protocol_login(storage, monkeypatch):
    store.finish_review_failure(storage["id"], "new@example.com", "unknown")
    store.delete_replacement(storage["replacement_id"])
    store.clear_finished_tasks()
    submit = Mock(return_value=1)
    monkeypatch.setattr(worker, "submit_tasks", submit)
    client = app.create_app(recover=False).test_client()
    response = client.post("/api/accounts/1/review-login", json={"transient_retries": 1})
    assert response.status_code == 202
    task = response.json["task"]
    assert task["new_email"] == "new@example.com"
    assert task["rebind_mode"] == "protocol"
    assert store.get_task_context(task["id"])["replacement"]["email"] == "new@example.com"
    assert client.post("/api/accounts/1/review-login").status_code == 409
    assert client.delete("/api/accounts/1").status_code == 409


@pytest.mark.parametrize("confirmed", [False, True])
def test_delete_finished_review_or_pending_account(storage, confirmed):
    if confirmed:
        confirmed_failure(storage)
    else:
        store.finish_review_failure(storage["id"], "new@example.com", "unknown")
    client = app.create_app(recover=False).test_client()
    assert client.delete("/api/accounts/1").status_code == 200
    assert store.list_accounts() == []
    assert store.list_replacements()[0]["status"] == ("used" if confirmed else "review")


def test_url_login_keeps_mail_api_after_pool_and_task_cleanup(storage):
    accounts = store._read(store._ACCOUNTS)
    accounts[0].update(password="", totp_secret="", auth_method="email_api")
    store._write(store._ACCOUNTS, accounts)
    store.finish_review_failure(storage["id"], "new@example.com", "unknown")
    store.delete_replacement(storage["replacement_id"])
    store.clear_finished_tasks()
    retry = store.reserve_review_login_retry(1)["task"]
    assert retry["rebind_mode"] == "browser"
    assert store.get_task_context(retry["id"])["replacement"]["api_url"] == "https://mail.example/new"


@pytest.mark.parametrize("outcome", ["confirmed", "rejected", "unknown"])
def test_migration_uses_actual_verify_response_after_cleanup(storage, tmp_path, outcome):
    store.finish_review_failure(storage["id"], "new@example.com", "old failure")
    store.delete_replacement(storage["replacement_id"])
    store.clear_finished_tasks()
    trace = [{"step": "login_old", "email": "old@example.com"}, {"step": "begin", "new_email": "new@example.com"}, {"step": "verify_pending"}]
    if outcome == "confirmed":
        trace.append({"step": "verify", "time": "2026-09-21T10:00:00"})
    trace.append({"step": "failed", "code": "VERIFY_FAILED" if outcome == "rejected" else "EXPORT_FAILED", "message": "verify HTTP 422 Invalid OTP" if outcome == "rejected" else "timeout"})
    folder = tmp_path / "traces" / "20260921_run"
    folder.mkdir(parents=True)
    (folder / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
    store.recover_rebind_accounts(folder.parent)
    assert store.list_accounts()[0]["status"] == {"confirmed": "pending_at", "rejected": "failed", "unknown": "review"}[outcome]
    assert store.recover_rebind_accounts(folder.parent) == {"pending_at": 0, "rejected": 0}


def test_explicit_invalid_otp_is_rejected_not_a_confirmed_change():
    result = protocol_flow.RebindResult(ok=False, code="VERIFY_FAILED", message='verify HTTP 422: {"detail":"Invalid OTP"}', trace=[{"step": "verify_pending"}])
    with pytest.raises(RuntimeError, match="换绑未生效") as error:
        protocol_flow._raise_upstream_failure(result, "new@example.com")
    assert not isinstance(error.value, roxy_flow.RebindOutcomeUnknown)


def test_reused_pool_id_does_not_redirect_login_to_another_email(storage, monkeypatch):
    confirmed_failure(storage)
    store.delete_replacement(storage["replacement_id"])
    store.import_replacement_emails("different@example.com----https://mail.example/different")
    login = Mock(return_value={"email": "new@example.com", "access_token": "new-at"})
    monkeypatch.setattr(protocol_flow, "refresh_access_token_protocol", login)
    retry = store.reserve_review_login_retry(1)["task"]
    worker._run(retry["id"])
    assert login.call_args.kwargs["email"] == "new@example.com"
    assert store.list_replacements()[0]["status"] == "available"
