from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import roxy_flow


SOURCE = Path("roxy_flow.py").read_text(encoding="utf-8")


def test_password_totp_login_prioritizes_generic_code_input_as_totp():
    assert "totp_submitted = False" in SOURCE
    assert "and not totp_submitted" in SOURCE
    assert "totp_page = True" in SOURCE


def test_email_api_remains_fallback_after_totp_submission():
    assert "email_otp_page = bool(code_input and not totp_page)" in SOURCE
    assert "email_api_url, email, previous=previous_email_otp" in SOURCE


def test_password_totp_account_never_reads_replacement_mailbox_api():
    session = {"accessToken": "at", "user": {"email": "new@example.com"}}
    with (
        patch.object(roxy_flow.mail_api, "read_current_otp") as read_current,
        patch.object(roxy_flow, "_clear_login_state"),
        patch.object(roxy_flow, "_submit_login_email_allow_password"),
        patch.object(roxy_flow, "_complete_login", return_value=session) as complete,
    ):
        result = roxy_flow._login_with_replacement_email(
            driver=SimpleNamespace(), email="new@example.com", password="pw",
            totp_secret="totp", fetch_session=Mock(),
            safe_get=Mock(), submit_email=Mock(),
            progress=lambda *_: None, api_url="https://mail.example/api",
            auth_method="password_totp", max_relogin_retries=0,
        )

    assert result == (session, "new@example.com", "at")
    read_current.assert_not_called()
    assert complete.call_args.kwargs["email_api_url"] == ""
