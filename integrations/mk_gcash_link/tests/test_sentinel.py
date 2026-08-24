import json
import unittest
from unittest.mock import patch

import sentinel


class SentinelProcessTests(unittest.TestCase):
    def test_node_bridge_is_started_without_a_windows_console(self):
        output = json.dumps({
            "main": json.dumps({"id": "device", "flow": "flow", "c": "fixture"}),
            "so": "",
        }).encode("utf-8")
        completed = sentinel.subprocess.CompletedProcess(
            args=["node", "sentinel_bridge.js"], returncode=0,
            stdout=output, stderr=b"",
        )
        with (
            patch.object(sentinel, "_resolve_node_executable", return_value="node"),
            patch.object(sentinel.subprocess, "run", return_value=completed) as run,
        ):
            result = sentinel.mint_sentinel_sync(
                flow="flow", device_id="device", user_agent="fixture", timeout_s=3,
            )

        self.assertEqual((json.loads(output.decode())["main"], ""), result)
        kwargs = run.call_args.kwargs
        self.assertIn("creationflags", kwargs)
        self.assertEqual(
            getattr(sentinel.subprocess, "CREATE_NO_WINDOW", 0) if sentinel.os.name == "nt" else 0,
            kwargs["creationflags"],
        )


if __name__ == "__main__":
    unittest.main()
