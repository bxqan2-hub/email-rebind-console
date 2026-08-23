# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

import roxy_flow


class CompleteLoginEmailApiTests(unittest.TestCase):
    def test_password_totp_account_allows_existing_password_page(self):
        driver = Mock(current_url="https://auth.openai.com/log-in/password")

        def reject_password_page(*_args, **_kwargs):
            raise RuntimeError(
                "邮箱提交后进入登录密码页，按已注册/不可用邮箱处理并停用"
            )

        with patch.object(roxy_flow.logger, "info") as info:
            state = roxy_flow._submit_login_email_allow_password(
                driver,
                "old@example.com",
                "account-password",
                "JBSWY3DPEHPK3PXP",
                reject_password_page,
            )

        self.assertEqual(state, "login_password")
        info.assert_called_once()

    def test_api_account_keeps_password_page_failure(self):
        driver = Mock(current_url="https://auth.openai.com/log-in/password")

        def reject_password_page(*_args, **_kwargs):
            raise RuntimeError("邮箱提交后进入登录密码页")

        with self.assertRaisesRegex(RuntimeError, "登录密码页"):
            roxy_flow._submit_login_email_allow_password(
                driver, "old@example.com", "", "", reject_password_page,
            )

    def test_complete_login_submits_password_then_totp(self):
        driver = Mock(current_url="https://auth.openai.com/log-in/password")
        state = {"step": "password"}
        submitted = []
        password_input = object()
        totp_input = object()

        def visible(_driver, selectors):
            if state["step"] == "password" and "input[type=\"password\"]" in selectors:
                return password_input
            if state["step"] == "totp" and 'input[name*="totp" i]' in selectors:
                return totp_input
            return None

        def set_value(_driver, element, value):
            submitted.append((element, value))

        def set_otp_value(_driver, element, value):
            submitted.append((element, value))

        def submit(_driver, element):
            if element is password_input:
                state["step"] = "totp"
            else:
                state["step"] = "logged_in"
                driver.current_url = "https://chatgpt.com/"

        def fetch_session(_driver, **_kwargs):
            if state["step"] == "logged_in":
                return {"accessToken": "at-password-totp"}
            raise RuntimeError("session not ready")

        with patch.object(roxy_flow, "_visible_input", side_effect=visible), \
                patch.object(roxy_flow, "_body_text", side_effect=lambda _driver: "Authenticator app" if state["step"] == "totp" else ""), \
                patch.object(roxy_flow, "_set_value", side_effect=set_value), \
                patch.object(roxy_flow, "_set_otp_value", side_effect=set_otp_value), \
                patch.object(roxy_flow, "_submit_near", side_effect=submit), \
                patch.object(roxy_flow.time, "sleep"), \
                patch.object(roxy_flow.pyotp.TOTP, "now", return_value="654321"):
            session = roxy_flow._complete_login(
                driver,
                "old@example.com",
                "account-password",
                "JBSWY3DPEHPK3PXP",
                fetch_session,
                Mock(),
                email_label="原邮箱",
            )

        self.assertEqual(session["accessToken"], "at-password-totp")
        self.assertEqual(submitted, [
            (password_input, "account-password"),
            (totp_input, "654321"),
        ])

    def test_email_api_login_fetches_and_submits_email_otp(self):
        driver = Mock()
        driver.current_url = "https://auth.openai.com/email-verification"
        code_input = object()
        progress = Mock()

        def submit(_driver, _element):
            driver.current_url = "https://chatgpt.com/"

        def visible(_driver, selectors):
            return None if 'input[type="password"]' in selectors else code_input

        with patch.object(roxy_flow, "_visible_input", side_effect=visible), \
                patch.object(roxy_flow, "_body_text", return_value="Verification code sent"), \
                patch.object(roxy_flow, "_set_otp_value") as set_otp_value, \
                patch.object(roxy_flow, "_submit_near", side_effect=submit), \
                patch.object(roxy_flow.mail_api, "wait_for_new_otp", return_value="123456") as wait_otp, \
                patch.object(roxy_flow.time, "sleep"):
            session = roxy_flow._complete_login(
                driver, "old@example.com", "", "",
                Mock(return_value={"accessToken": "at-old"}), progress,
                email_api_url="https://mail.example/old",
                previous_email_otp="654321", email_label="原邮箱",
            )

        self.assertEqual(session["accessToken"], "at-old")
        wait_otp.assert_called_once()
        self.assertEqual(wait_otp.call_args.args[:2], ("https://mail.example/old", "old@example.com"))
        self.assertEqual(wait_otp.call_args.kwargs["previous"], "654321")
        set_otp_value.assert_called_once_with(driver, code_input, "123456")
        progress.assert_any_call("login_email_otp", "通过原邮箱 API 等待登录验证码")


if __name__ == "__main__":
    unittest.main()
