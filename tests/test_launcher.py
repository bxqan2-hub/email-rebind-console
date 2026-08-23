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
        self.assertIn('set "EMAIL_REBIND_PORT=5092"', text)
        self.assertIn('set "APP_URL=http://127.0.0.1:5092/"', text)
        self.assertIn('set "HEALTH_URL=http://127.0.0.1:5092/health"', text)
        self.assertIn('pythonw.exe', text)
        self.assertIn('call :healthcheck', text)
        self.assertIn('Invoke-WebRequest', text)
        self.assertIn('for /l %%i in (1,1,30)', text)
        self.assertIn('Start-Sleep -Seconds 1', text)
        self.assertIn('goto :open', text)
        self.assertIn('start "" "%APP_URL%"', text)

    def test_stop_bat_targets_the_same_port(self):
        text = Path("stop.bat").read_text(encoding="utf-8")
        self.assertIn('findstr ":5092"', text)
        self.assertNotIn(":5091", text)

    def test_normal_polling_requests_are_not_logged_to_console(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn('logging.getLogger("werkzeug").setLevel(logging.WARNING)', source)


if __name__ == "__main__":
    unittest.main()
