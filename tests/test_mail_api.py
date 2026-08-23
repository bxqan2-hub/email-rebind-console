# -*- coding: utf-8 -*-
import json
import unittest
from unittest.mock import Mock, patch

import mail_api


class MailApiTests(unittest.TestCase):
    def test_extract_otp_prefers_named_json_field(self):
        body = json.dumps({"messages": [{"subject": "Order 123456"}], "verification_code": "654321"})
        self.assertEqual(mail_api.extract_otp(body), "654321")

    def test_extract_otp_reads_latest_mail_card_before_css_and_uuid_numbers(self):
        body = '''<style>.x{color:#123456}</style>
        <a class="mail"><strong>ChatGPT</strong><span class="code">045542</span></a>
        <a class="mail"><strong>OpenAI</strong><span class="code">527593</span></a>'''
        self.assertEqual(mail_api.extract_otp(body), "045542")

    def test_extract_otp_reads_openai_subject_when_code_node_is_missing(self):
        body = "<div>OpenAI</div><div>Your OpenAI code is 527593</div>"
        self.assertEqual(mail_api.extract_otp(body), "527593")

    def test_extract_otp_decodes_mail_body_embedded_in_javascript(self):
        body = r'''<style>.theme{color:#495057}</style>
        <div>Your temporary ChatGPT login code</div>
        <script>
          var htmlContent = "<html><body><p>Enter this temporary verification code to continue:</p><p style=\"font-size:24px\">830982</p></body></html>";
          document.write(htmlContent);
        </script>'''
        self.assertEqual(mail_api.extract_otp(body), "830982")

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
