# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import Mock, patch

import mail_api


class MailApiTests(unittest.TestCase):
    def test_extract_otp_prefers_named_json_field(self):
        body = json.dumps({"messages": [{"subject": "Order 123456"}], "verification_code": "654321"})
        self.assertEqual(mail_api.extract_otp(body), "654321")

    @patch("mail_api.requests.get")
    def test_read_current_otp_uses_configured_tls_verification(self, get: Mock):
        response = Mock(text='{"otp":"112233"}')
        response.raise_for_status.return_value = None
        get.return_value = response
        with patch.object(mail_api.settings, "MAIL_VERIFY_TLS", True):
            self.assertEqual(mail_api.read_current_otp("https://mail.example/code", "new@example.com"), "112233")
        self.assertTrue(get.call_args.kwargs["verify"])


if __name__ == "__main__":
    unittest.main()
