# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

import roxy_flow


class CompleteLoginEmailApiTests(unittest.TestCase):
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
