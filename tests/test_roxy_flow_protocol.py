# -*- coding: utf-8 -*-
import base64
import json
import unittest
from unittest.mock import Mock, call, patch

import roxy_flow


def _session(account_id="account-test"):
    payload = {
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return {"accessToken": f"header.{encoded}.signature"}


class ProtocolEmailChangeTests(unittest.TestCase):
    def test_capture_content_lengths_match_new_email_and_code_payloads(self):
        # 脱敏构造一个与抓包邮箱长度相同的值，验证 53/69 字节请求体结构。
        email = "a" * 29 + "@example.com"
        begin = json.dumps({"email": email}, separators=(",", ":"))
        verify = json.dumps({"email": email, "code": "1" * 6}, separators=(",", ":"))
        self.assertEqual(len(begin), 53)
        self.assertEqual(len(verify), 69)

    def test_browser_request_uses_roxy_cookies_without_exporting_token(self):
        driver = Mock()
        driver.execute_async_script.return_value = {"ok": True, "status": 200, "data": {"success": True}}
        result = roxy_flow._browser_json_request(
            driver, "POST", "/backend-api/accounts/change_email/begin",
            account_id="account-test", body={"email": "new@example.com"},
        )

        args = driver.execute_async_script.call_args.args
        self.assertEqual(args[1:], (
            "POST", "/backend-api/accounts/change_email/begin", "account-test",
            {"email": "new@example.com"}, 45000.0,
        ))
        self.assertIn("credentials:'include'", args[0])
        self.assertNotIn("authorization", args[0].lower())
        self.assertEqual(result["status"], 200)

    def test_protocol_uses_eligibility_begin_verify_without_settings(self):
        driver = Mock(current_url="https://chatgpt.com/")
        progress = Mock()
        responses = [
            {"ok": True, "status": 200, "data": {"eligible": True, "eligibility_type": "password"}},
            {"ok": True, "status": 200, "data": {"success": True}},
            {"ok": True, "status": 200, "data": {"success": True}},
        ]
        with patch.object(roxy_flow, "_browser_json_request", side_effect=responses) as request, \
                patch.object(roxy_flow.mail_api, "read_current_otp", return_value="111111"), \
                patch.object(roxy_flow.mail_api, "wait_for_new_otp", return_value="222222") as wait:
            roxy_flow._change_email_via_protocol(
                driver,
                session=_session(),
                new_email="new@example.com",
                api_url="https://mail.example/new",
                progress=progress,
            )

        self.assertEqual(request.call_args_list, [
            call(
                driver, "GET", "/backend-api/accounts/change_email/eligibility",
            ),
            call(
                driver, "POST", "/backend-api/accounts/change_email/begin",
                account_id="account-test", body={"email": "new@example.com"},
            ),
            call(
                driver, "POST", "/backend-api/accounts/change_email/verify",
                account_id="account-test",
                body={"email": "new@example.com", "code": "222222"},
            ),
        ])
        wait.assert_called_once()
        progress.assert_any_call("protocol_eligibility", "登录成功；通过协议检查邮箱换绑资格")
        progress.assert_any_call("protocol_begin", "通过协议提交替换邮箱并请求验证码")
        progress.assert_any_call("protocol_verify", "通过协议提交替换邮箱验证码")
        progress.assert_any_call("changed", "协议已确认邮箱更新；准备清理旧登录态")
        driver.execute_script.assert_not_called()

    def test_protocol_route_unavailable_never_falls_back_to_settings(self):
        driver = Mock(current_url="https://chatgpt.com/")
        responses = [
            {"ok": True, "status": 200, "data": {"eligible": True, "eligibility_type": "password"}},
            {"ok": True, "status": 404, "data": {}},
        ]
        with patch.object(roxy_flow, "_browser_json_request", side_effect=responses), \
                patch.object(roxy_flow.mail_api, "read_current_otp", return_value=None):
            with self.assertRaisesRegex(roxy_flow.ProtocolChangeFailure, "开始接口不可用"):
                roxy_flow._change_email_via_protocol(
                    driver, session=_session(), new_email="new@example.com",
                    api_url="https://mail.example/new", progress=Mock(),
                )
        driver.execute_script.assert_not_called()

    def test_social_password_payload_preserves_remove_social_subs(self):
        session = _session()
        responses = [
            {"ok": True, "status": 200, "data": {"eligible": True, "eligibility_type": "social_password"}},
            {"ok": True, "status": 200, "data": {"success": True}},
            {"ok": True, "status": 200, "data": {"success": True}},
        ]
        with patch.object(roxy_flow, "_browser_json_request", side_effect=responses) as request, \
                patch.object(roxy_flow.mail_api, "read_current_otp", return_value=None), \
                patch.object(roxy_flow.mail_api, "wait_for_new_otp", return_value="222222"):
            roxy_flow._change_email_via_protocol(
                Mock(), session=session, new_email="new@example.com",
                api_url="https://mail.example/new", progress=Mock(),
            )

        self.assertEqual(request.call_args_list[1].kwargs["body"], {
            "email": "new@example.com", "remove_social_subs": True,
        })
        self.assertEqual(request.call_args_list[2].kwargs["body"], {
            "email": "new@example.com", "code": "222222", "remove_social_subs": True,
        })

    def test_social_account_requires_password_before_begin(self):
        with patch.object(roxy_flow, "_browser_json_request", return_value={
            "ok": True, "status": 200,
            "data": {"eligible": True, "eligibility_type": "social"},
        }) as request:
            with self.assertRaisesRegex(RuntimeError, "Security 中设置密码"):
                roxy_flow._change_email_via_protocol(
                    Mock(), session=_session(), new_email="new@example.com",
                    api_url="https://mail.example/new", progress=Mock(),
                )
        request.assert_called_once()

    def test_ineligible_account_stops_before_begin(self):
        driver = Mock(current_url="https://chatgpt.com/")
        with patch.object(roxy_flow, "_browser_json_request", return_value={
            "ok": True, "status": 200, "data": {"eligible": False},
        }) as request:
            with self.assertRaisesRegex(RuntimeError, "不符合自助换绑条件"):
                roxy_flow._change_email_via_protocol(
                    driver,
                    session=_session(),
                    new_email="new@example.com",
                    api_url="https://mail.example/new",
                    progress=Mock(),
                )
        request.assert_called_once()

    def test_verify_transport_failure_freezes_unknown_outcome(self):
        driver = Mock(current_url="https://chatgpt.com/")
        responses = [
            {"ok": True, "status": 200, "data": {"eligible": True}},
            {"ok": True, "status": 200, "data": {"success": True}},
            {"ok": False, "status": 0, "error": "request_timeout"},
        ]
        with patch.object(roxy_flow, "_browser_json_request", side_effect=responses), \
                patch.object(roxy_flow.mail_api, "read_current_otp", return_value=None), \
                patch.object(roxy_flow.mail_api, "wait_for_new_otp", return_value="222222"):
            with self.assertRaises(roxy_flow.RebindOutcomeUnknown):
                roxy_flow._change_email_via_protocol(
                    driver,
                    session=_session(),
                    new_email="new@example.com",
                    api_url="https://mail.example/new",
                    progress=Mock(),
                )

    def test_verify_403_is_classified_as_email_in_use(self):
        responses = [
            {"ok": True, "status": 200, "data": {"eligible": True, "eligibility_type": "password"}},
            {"ok": True, "status": 200, "data": {"success": True}},
            {"ok": True, "status": 403, "data": {}},
        ]
        with patch.object(roxy_flow, "_browser_json_request", side_effect=responses), \
                patch.object(roxy_flow.mail_api, "read_current_otp", return_value=None), \
                patch.object(roxy_flow.mail_api, "wait_for_new_otp", return_value="222222"):
            with self.assertRaises(roxy_flow.ReplacementEmailFailure) as raised:
                roxy_flow._change_email_via_protocol(
                    Mock(), session=_session(), new_email="new@example.com",
                    api_url="https://mail.example/new", progress=Mock(),
                )
        self.assertEqual(raised.exception.code, "email_in_use")

    def test_begin_403_is_classified_as_email_in_use(self):
        responses = [
            {"ok": True, "status": 200, "data": {"eligible": True, "eligibility_type": "password"}},
            {"ok": True, "status": 403, "data": {}},
        ]
        with patch.object(roxy_flow, "_browser_json_request", side_effect=responses), \
                patch.object(roxy_flow.mail_api, "read_current_otp", return_value=None):
            with self.assertRaises(roxy_flow.ReplacementEmailFailure) as raised:
                roxy_flow._change_email_via_protocol(
                    Mock(), session=_session(), new_email="new@example.com",
                    api_url="https://mail.example/new", progress=Mock(),
                )
        self.assertEqual(raised.exception.code, "email_in_use")

    def test_nested_api_error_code_is_preserved(self):
        self.assertEqual(
            roxy_flow._api_error_code({"detail": {"error": {"code": "reauth_required"}}}),
            "reauth_required",
        )

    def test_segmented_otp_fills_each_box_without_single_input_fallback(self):
        driver = Mock()
        driver.execute_script.return_value = 6
        anchor = Mock()
        with patch.object(roxy_flow, "_set_value") as fallback:
            roxy_flow._set_otp_value(driver, anchor, "123456")
        fallback.assert_not_called()
        self.assertIn("Number(el.maxLength) === 1", driver.execute_script.call_args.args[0])
        self.assertIn('data-testid="modal-add-email-otp"', driver.execute_script.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
