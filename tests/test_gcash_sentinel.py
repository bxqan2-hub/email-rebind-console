import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


UPSTREAM_ROOT = Path(__file__).resolve().parents[1] / "integrations" / "mk_gcash_link"
_spec = importlib.util.spec_from_file_location("gcash_sentinel_test_module", UPSTREAM_ROOT / "sentinel.py")
sentinel = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(sentinel)


def test_windows_node_bridge_uses_create_no_window():
    payload = {"id": "device-1", "flow": "chat", "c": "proof"}
    completed = SimpleNamespace(
        stdout=json.dumps({"main": json.dumps(payload), "so": ""}).encode(),
        stderr=b"",
    )
    with (
        patch.object(sentinel, "_resolve_node_executable", return_value="node"),
        patch.object(sentinel.subprocess, "run", return_value=completed) as run,
        patch.object(sentinel.os, "name", "nt"),
    ):
        result = sentinel.mint_sentinel_sync(
            flow="chat",
            device_id="device-1",
            user_agent="fixture",
        )

    assert result == (json.dumps(payload), "")
    assert run.call_args.kwargs["creationflags"] == getattr(
        sentinel.subprocess, "CREATE_NO_WINDOW", 0
    )


def test_curl_child_process_hides_windows_console():
    source = (UPSTREAM_ROOT / "sentinel_bridge.js").read_text(encoding="utf-8")
    assert "windowsHide: true" in source
