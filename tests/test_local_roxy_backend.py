"""Local browser routing without a running Roxy service or a sibling checkout."""
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

import roxy_browser_rebind
import roxy_flow


@pytest.fixture
def upstream_modules(monkeypatch):
    config = ModuleType("config")
    config.roxybrowser = SimpleNamespace(
        ROXY_LOCAL_COMPONENT=False,
        ROXY_OPEN_PATH="/official/open/{profile_id}",
        ROXY_OPEN_METHOD="GET",
        ROXY_OPEN_EXTRA_PARAMS={"dirId": "stale-id", "headless": True, "args": []},
    )
    core = ModuleType("core")
    client_module = ModuleType("core.roxybrowser_client")

    def first(payload, paths):
        for path in paths:
            value = payload
            for name in path:
                value = value.get(name) if isinstance(value, dict) else None
            if value is not None:
                return value
        return None

    client_module._first = first
    client_module._workspace_id_value = lambda: "official-workspace"
    client_module.RoxyOpenResult = lambda profile_id, raw, **kw: SimpleNamespace(
        profile_id=profile_id, raw=raw, **kw,
    )
    core.roxybrowser_client = client_module
    monkeypatch.setitem(sys.modules, "config", config)
    monkeypatch.setitem(sys.modules, "core", core)
    monkeypatch.setitem(sys.modules, "core.roxybrowser_client", client_module)
    return config.roxybrowser


@pytest.mark.parametrize("module,entry,extra", [
    (roxy_flow, "perform_replacement_login", {}),
    (roxy_browser_rebind, "perform_replacement_login", {}),
    (roxy_browser_rebind, "perform_email_rebind", {"old_email": "old@example.com"}),
])
def test_new_window_entry_points_force_local_visible_browser(monkeypatch, module, entry, extra):
    client = Mock()
    client.open_profile.side_effect = RuntimeError("open-probe")
    client_type = Mock(return_value=client)
    monkeypatch.setattr(module, "_load_main_roxy", lambda: (client_type, *([None] * 6)))

    with pytest.raises(RuntimeError, match="open-probe"):
        getattr(module, entry)(
            new_email="new@example.com", password="test-password", totp_secret="test-secret",
            api_url="https://mail.example/code", proxy_url=" http://proxy.example:8080 ",
            **extra,
        )

    client_type.assert_called_once_with(
        local_component=True, profile_proxy="http://proxy.example:8080",
    )
    client.open_profile.assert_called_once_with(require_proxy_exit_ip=True, headless=False)
    client.close_profile.assert_not_called()
    client.delete_profile.assert_not_called()


def test_local_reopen_ignores_official_path_and_stale_dir_id(upstream_modules):
    upstream_modules.ROXY_OPEN_PATH = "http://127.0.0.1:50100/official/open"
    upstream_modules.ROXY_OPEN_EXTRA_PARAMS["args"] = [" --extra ", "--extra", None]
    client = Mock(local_component=True, _local_startup_args=["--local-arg"])
    client.request.return_value = {"data": {"ws": "ws://127.0.0.1:9222/devtools/browser/test"}}
    client._extract_debugger_address.return_value = "127.0.0.1:9222"

    opened = roxy_flow._open_existing_profile(client, "local-abc123")

    client.request.assert_called_once_with(
        "POST", "/browser/open", params=None,
        json_body={
            "dirId": "abc123", "headless": False,
            "args": ["--local-arg", "--extra"], "workbench": False,
        },
    )
    assert opened.profile_id == "local-abc123"
    assert opened.created_by_run is False
    assert opened.ws_endpoint == "ws://127.0.0.1:9222/devtools/browser/test"


@pytest.mark.parametrize("extra", [None, {"args": "--invalid-list"}])
def test_local_reopen_tolerates_empty_or_invalid_extra_args(upstream_modules, extra):
    upstream_modules.ROXY_OPEN_EXTRA_PARAMS = extra
    client = Mock(local_component=True, _local_startup_args=[])
    client.request.return_value = {}
    client._extract_debugger_address.return_value = "127.0.0.1:9222"
    roxy_flow._open_existing_profile(client, "local-abc123")
    assert client.request.call_args.kwargs["json_body"]["args"] == []


def test_legacy_profile_reopen_keeps_original_backend_and_selected_id(upstream_modules):
    client = Mock(local_component=False)
    client.request.return_value = {}
    client._extract_debugger_address.return_value = "127.0.0.1:9222"
    opened = roxy_flow._open_existing_profile(client, "123")
    client.request.assert_called_once_with(
        "GET", "/official/open/123", json_body=None,
        params={
            "workspaceId": "official-workspace", "dirId": 123,
            "headless": False, "args": [], "forceOpen": True,
        },
    )
    assert opened.profile_id == "123"


@pytest.mark.parametrize("profile_id,is_local", [("local-abc123", False), ("123", True)])
def test_reopen_rejects_wrong_backend_before_sending_request(upstream_modules, profile_id, is_local):
    client = Mock(local_component=is_local)
    with pytest.raises(ValueError, match="所属后端"):
        roxy_flow._open_existing_profile(client, profile_id)
    client.request.assert_not_called()


@pytest.mark.parametrize("module", [roxy_flow, roxy_browser_rebind])
@pytest.mark.parametrize("profile_id,is_local", [("local-abc123", True), ("123", False)])
def test_saved_profile_lifecycle_after_restart(monkeypatch, upstream_modules, module, profile_id, is_local):
    client = Mock(local_component=is_local, _local_startup_args=[])
    client.request.return_value = {}
    client._extract_debugger_address.return_value = "127.0.0.1:9222"
    client.close_profile.return_value = True
    client.delete_profile.return_value = True
    client_type = Mock(return_value=client)
    driver = Mock()
    session = {"user": {"email": "new@example.com"}, "accessToken": "at-refreshed"}
    loaded = (client_type, Mock(return_value=driver), Mock(), Mock(return_value=session), None, None, None)
    monkeypatch.setattr(module, "_load_main_roxy", lambda: loaded)
    monkeypatch.setattr(module, "_RETAINED", {})
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    assert module.resolve_roxy_cdp_port(profile_id) == 9222
    result = module.refresh_retained_access_token(profile_id, "new@example.com")
    assert result["access_token"] == "at-refreshed"
    assert result["roxy_cdp_port"] == 9222
    assert client_type.call_count == 2
    for call in client_type.call_args_list:
        assert call.kwargs == {"local_component": is_local}
    for call in client.request.call_args_list:
        if is_local:
            assert call.args == ("POST", "/browser/open")
            assert call.kwargs["json_body"]["dirId"] == "abc123"
        else:
            assert call.args == ("GET", "/official/open/123")

    assert module.delete_retained_profile(profile_id) is True
    client.close_profile.assert_called_once_with(profile_id)
    client.delete_profile.assert_called_once_with(profile_id)
    driver.quit.assert_called_once()
    assert not module._RETAINED

    # The delete button also works before a saved window has been reattached.
    client_type.reset_mock()
    client.close_profile.reset_mock()
    client.delete_profile.reset_mock()
    assert module.delete_retained_profile(profile_id) is True
    client_type.assert_called_once_with(local_component=is_local)
    client.close_profile.assert_called_once_with(profile_id)
    client.delete_profile.assert_called_once_with(profile_id)
