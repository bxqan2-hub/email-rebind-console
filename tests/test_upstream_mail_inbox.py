# -*- coding: utf-8 -*-
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations" / "chatgpt_rebind_standalone"))
from rebind_core.mail_inbox import extract_otp_from_text


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


if __name__ == "__main__":
    unittest.main()
