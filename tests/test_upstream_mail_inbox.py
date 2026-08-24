# -*- coding: utf-8 -*-
import unittest
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "chatgpt_rebind_standalone"))
from rebind_core.mail_inbox import extract_otp_from_text, fetch_latest_otp


class UpstreamMailInboxTests(unittest.TestCase):
    def test_html_mailbox_uses_newest_code_card(self):
        body = """<style>.x{color:#123456}</style>
        <a class="mail"><strong>OpenAI</strong><span class="code">017856</span></a>
        <a class="mail"><strong>OpenAI</strong><span class="code">407759</span></a>"""
        self.assertEqual(extract_otp_from_text(body), "017856")

    def test_subject_fallback_uses_first_visible_code(self):
        body = """<style>.x{color:#123456}</style>
        <div>Your OpenAI code is 017856</div>
        <div>Your OpenAI code is 407759</div>"""
        self.assertEqual(extract_otp_from_text(body), "017856")

    def test_plain_text_still_uses_first_code(self):
        self.assertEqual(extract_otp_from_text("OpenAI code: 017856; old: 407759"), "017856")

    @patch("rebind_core.mail_inbox.requests.get")
    def test_html_mailbox_waits_when_all_cards_predate_begin(self, get: Mock):
        response = Mock(
            text='''<a class="mail"><span>2026/08/24 22:32</span>
            <span class="code">017856</span></a>''',
            headers={"content-type": "text/html; charset=utf-8"},
        )
        response.raise_for_status.return_value = None
        get.return_value = response
        issued_after = datetime.strptime("2026/08/24 22:50:08", "%Y/%m/%d %H:%M:%S").timestamp()

        self.assertEqual(fetch_latest_otp("https://mail.example/inbox", issued_after=issued_after), "")

    @patch("rebind_core.mail_inbox.requests.get")
    def test_html_mailbox_accepts_new_card_in_begin_minute(self, get: Mock):
        response = Mock(
            text='''<a class="mail"><span>2026/08/24 22:50</span>
            <span class="code">991232</span></a>
            <a class="mail"><span>2026/08/24 22:32</span>
            <span class="code">017856</span></a>''',
            headers={"content-type": "text/html; charset=utf-8"},
        )
        response.raise_for_status.return_value = None
        get.return_value = response
        issued_after = datetime.strptime("2026/08/24 22:50:08", "%Y/%m/%d %H:%M:%S").timestamp()

        self.assertEqual(fetch_latest_otp("https://mail.example/inbox", issued_after=issued_after), "991232")


if __name__ == "__main__":
    unittest.main()
