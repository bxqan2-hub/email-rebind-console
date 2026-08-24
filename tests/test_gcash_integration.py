# -*- coding: utf-8 -*-
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import gcash_service


class GCashIntegrationTests(unittest.TestCase):
    def test_vendored_source_is_locked_and_present(self):
        lock = json.loads(Path("integrations/upstream-lock.json").read_text(encoding="utf-8"))
        item = lock["runtime_projects"][0]
        self.assertEqual(item["commit"], gcash_service.UPSTREAM_COMMIT)
        self.assertTrue((gcash_service.UPSTREAM_ROOT / "app.py").is_file())
        self.assertTrue((gcash_service.UPSTREAM_ROOT / "gcash_chain.py").is_file())
        self.assertTrue((gcash_service.UPSTREAM_ROOT / "web" / "index.html").is_file())
        self.assertTrue((gcash_service.UPSTREAM_ROOT / "LICENSE").is_file())
        source = (gcash_service.UPSTREAM_ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("frame-ancestors {EMBED_ORIGIN}", source)
        self.assertNotIn('self.send_header("X-Frame-Options", "DENY")', source)

    def test_left_navigation_and_lazy_iframe(self):
        with patch.object(gcash_service, "status", return_value={"running": False}):
            client = app.create_app(recover=False).test_client()
            html = client.get("/").get_data(as_text=True)
        script = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn('data-view="gcash"', html)
        self.assertIn('id="view-gcash"', html)
        self.assertIn('id="gcashFrame"', html)
        self.assertIn("frame.dataset.src", script)
        self.assertIn("data-push-gcash", script)
        self.assertIn("pushAccessTokenToGCash", script)
        self.assertIn("pushSelectedAccessTokensToGCash", script)
        upstream_script = (gcash_service.UPSTREAM_ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('email-rebind:push-at', upstream_script)
        self.assertIn('event.source !== window.parent', upstream_script)
        self.assertIn('event.origin !== REBIND_PARENT_ORIGIN', upstream_script)
        self.assertIn("Array.isArray(payload.accounts)", upstream_script)
        self.assertIn('id="jobConcurrency"', (gcash_service.UPSTREAM_ROOT / "web" / "index.html").read_text(encoding="utf-8"))

    def test_health_exposes_gcash_companion_status(self):
        expected = {"running": True, "port": 8931}
        with patch.object(gcash_service, "status", return_value=expected):
            body = app.create_app(recover=False).test_client().get("/health").get_json()
        self.assertEqual(body["gcash"], expected)


if __name__ == "__main__":
    unittest.main()
