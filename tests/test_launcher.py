# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class LauncherTests(unittest.TestCase):
    def test_start_bat_is_windows_safe_and_idempotent(self):
        raw = Path("start.bat").read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        text = raw.decode("ascii")
        self.assertIn('cd /d "%~dp0"', text)
        self.assertIn('findstr ":5091"', text)
        self.assertIn('if errorlevel 1 start "Email Rebind Console"', text)
        self.assertIn('start "" "http://127.0.0.1:5091/"', text)


if __name__ == "__main__":
    unittest.main()
