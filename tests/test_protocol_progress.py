"""Regression coverage for protocol latency, live stages and commit boundaries."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import protocol_flow
import store
import worker
from rebind_core import mail_inbox, mfa_login, pipeline


@pytest.fixture
def task_store(tmp_path, monkeypatch):
    for key in ("_ACCOUNTS", "_REPLACEMENTS", "_TASKS", "_PROXIES", "_DETECTION_PROXIES"):
        monkeypatch.setattr(store, key, tmp_path / f"{key}.json")
    monkeypatch.setattr(worker, "submit_trial_check", Mock())
    store.import_source_accounts("old@example.com----Password!----JBSWY3DPEHPK3PXP")
    store.import_replacement_emails("new@example.com----https://mail.example/new")
    store.import_proxies("http://proxy.example:8080")
    return store.reserve_batch()[0]


def test_mail_timeout_does_not_disable_a_working_proxy(task_store, monkeypatch):
    def failed(**kwargs):
        protocol_flow._raise_upstream_failure(
            pipeline.RebindResult(ok=False, code="MAIL_TIMEOUT", message="150s timeout"),
            kwargs["new_email"],
        )
    monkeypatch.setattr(protocol_flow, "run_upstream_rebind", failed)
    worker._run(task_store["id"])
    assert store.list_proxies()[0]["status"] == "available"
    assert store.list_replacements()[0]["status"] == "failed"
    assert store.list_tasks()[0]["proxy_attempt"] == 1


@pytest.mark.parametrize("step", ["verify_pending", "verify"])
@pytest.mark.parametrize("message", ["network timeout", "invalid_state", "email already in use"])
def test_post_submit_failure_never_rotates_proxy_or_email(step, message):
    result = pipeline.RebindResult(ok=False, code="LOGIN_FAILED", message=message, trace=[{"step": step}])
    with pytest.raises(protocol_flow.RebindOutcomeUnknown):
        protocol_flow._raise_upstream_failure(result, "new@example.com")


def test_heartbeat_preserves_stage_start_and_records_transitions(task_store, monkeypatch):
    now = Mock(side_effect=["2026-09-20T18:00:00", "2026-09-20T18:00:04", "2026-09-20T18:00:07"])
    monkeypatch.setattr(store, "_now", now)
    for stage, message in [("wait_new_email_otp", "query 1"), ("wait_new_email_otp", "query 2"), ("submit_new_email_otp", "submit")]:
        store.update_task(task_store["id"], status="running", stage=stage, message=message)
    task = store.list_tasks()[0]
    assert len(task["stage_history"]) == 2
    assert task["stage_history"][0]["started_at"] == "2026-09-20T18:00:00"
    assert task["stage_started_at"] == "2026-09-20T18:00:07"


def test_stop_after_submit_keeps_uncertain_identity_reserved(task_store):
    store.update_task(task_store["id"], status="running", stage="submit_new_email_otp")
    store.request_task_stop(task_store["id"])
    store.finish_stopped(task_store["id"])
    assert store.list_accounts()[0]["status"] == "review"
    assert store.list_accounts()[0]["current_email"] == "new@example.com"
    assert store.list_replacements()[0]["status"] == "review"


def test_stop_during_verify_timeout_does_not_release_replacement(task_store, monkeypatch):
    def uncertain(**kwargs):
        kwargs["progress"]("submit_new_email_otp", "submitting")
        store.request_task_stop(task_store["id"])
        raise protocol_flow.RebindOutcomeUnknown("new@example.com", "verify timeout")
    monkeypatch.setattr(protocol_flow, "run_upstream_rebind", uncertain)
    worker._run(task_store["id"])
    assert store.list_accounts()[0]["status"] == "review"
    assert store.list_replacements()[0]["status"] == "review"


def test_stop_after_confirmed_change_still_saves_success(task_store, monkeypatch):
    def success(**kwargs):
        store.request_task_stop(task_store["id"])
        for stage in ("changed", "protocol_relogin_new", "protocol_export", "protocol_verified"):
            kwargs["progress"](stage, "confirmed")
        return {"email": "new@example.com", "access_token": "test-at"}
    monkeypatch.setattr(protocol_flow, "run_upstream_rebind", success)
    worker._run(task_store["id"])
    assert store.list_tasks()[0]["status"] == "success"
    assert store.get_success_access_token(1) == "test-at"


def test_mail_deadline_bounds_request_and_sleep_and_redacts_url(monkeypatch):
    clock = [0.0]
    request_timeouts, messages = [], []
    monkeypatch.setattr(mail_inbox.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(mail_inbox.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def fetch(*args, timeout, **kwargs):
        request_timeouts.append(timeout)
        clock[0] += timeout
        raise TimeoutError("https://mail.example/?token=PRIVATE")
    monkeypatch.setattr(mail_inbox, "fetch_latest_otp", fetch)
    with pytest.raises(TimeoutError) as error:
        mail_inbox.wait_code("https://mail.example", timeout=15, poll_interval=2, progress=messages.append)
    assert clock[0] == 15
    assert request_timeouts == [8, 5]
    assert len(messages) == 2
    assert "PRIVATE" not in str(error.value) + "".join(messages)
    assert "TimeoutError" in messages[-1]


def test_json_mailbox_does_not_reuse_filtered_old_code(monkeypatch):
    payload = {"messages": [{"timestamp": 100, "code": "123456"}]}
    response = Mock(text=json.dumps(payload), headers={"content-type": "application/json"})
    response.json.return_value = payload
    monkeypatch.setattr(mail_inbox.requests, "get", Mock(return_value=response))
    assert mail_inbox.fetch_latest_otp("https://mail.example", issued_after=200) == ""


@pytest.mark.parametrize("session_ready", [True, False])
def test_session_at_skips_exchange_but_missing_at_keeps_fallback(session_ready):
    auth = Mock()
    auth.result = SimpleNamespace(access_token="", session_token="", is_valid=lambda: bool(auth.result.access_token))
    auth._normalize_continue_url.side_effect = lambda value: value
    auth.follow_redirect_chain.return_value = ("https://callback.example/?code=test", "https://app.example")
    def fetch_session():
        if session_ready or auth.oauth_token_exchange.called:
            auth.result.access_token = "test-at"
    auth.get_auth_session.side_effect = fetch_session
    assert mfa_login._finish_to_session(auth, "https://continue.example").access_token == "test-at"
    assert auth.oauth_token_exchange.call_count == (0 if session_ready else 1)


def test_pipeline_streams_steps_before_blocking_calls(tmp_path, monkeypatch):
    stages = []
    monkeypatch.setattr(pipeline, "ROOT", tmp_path)
    def login(email, *args, progress, **kwargs):
        assert stages[-1] == ("protocol_login_old" if email.startswith("old") else "protocol_relogin_new")
        progress("验证 2FA 动态码")
        return SimpleNamespace(account_id="account", access_token="test-at", factor_id="factor")
    monkeypatch.setattr(pipeline, "login_with_password_and_totp", login)
    client = Mock()
    client.eligibility.side_effect = lambda: {"eligible": stages[-1] == "check_email_eligibility"}
    client.begin.side_effect = lambda email: {"ok": stages[-1] == "submit_new_email"}
    client.verify.side_effect = lambda email, code: {"ok": stages[-1] == "submit_new_email_otp"}
    monkeypatch.setattr(pipeline, "ChangeEmailClient", Mock(return_value=client))
    def wait(*args, progress, **kwargs):
        progress("等待验证码")
        return "123456"
    monkeypatch.setattr(pipeline, "wait_code", wait)
    monkeypatch.setattr(pipeline, "build_login_bundle", Mock(return_value={"email": "new@example.com"}))
    monkeypatch.setattr(pipeline, "write_login_bundle", Mock(return_value={"bundle": tmp_path / "bundle.json"}))
    result = pipeline.run_rebind_email(
        old_email="old@example.com", new_email="new@example.com", password="test", totp_secret="test",
        mail_api="https://mail.example", progress=lambda stage, message: stages.append(stage),
    )
    assert result.ok
    assert list(dict.fromkeys(stages)) == [
        "protocol_login_old", "check_email_eligibility", "submit_new_email", "wait_new_email_otp",
        "submit_new_email_otp", "changed", "protocol_relogin_new", "protocol_export",
    ]
    assert {item["step"] for item in result.trace} >= {"verify_pending", "verify", "export"}
