import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


UPSTREAM_ROOT = Path(__file__).resolve().parents[1] / "integrations" / "chatgpt_rebind_standalone"
if str(UPSTREAM_ROOT) not in sys.path:
    sys.path.insert(0, str(UPSTREAM_ROOT))

from registration_core import sentinel_quickjs


def test_windows_quickjs_node_process_uses_create_no_window(tmp_path):
    completed = SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")
    with (
        patch.object(sentinel_quickjs, "_resolve_node_binary", return_value="node"),
        patch.object(sentinel_quickjs.subprocess, "run", return_value=completed) as run,
        patch.object(sentinel_quickjs.os, "name", "nt"),
    ):
        result = sentinel_quickjs._run_quickjs_action(
            action="requirements",
            sdk_file=tmp_path / "sdk.js",
            quickjs_script=tmp_path / "adapter.js",
            payload={"device_id": "fixture"},
            timeout_ms=1000,
        )

    assert result == {"ok": True}
    assert run.call_args.kwargs["creationflags"] == getattr(
        sentinel_quickjs.subprocess, "CREATE_NO_WINDOW", 0
    )
