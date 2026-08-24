# -*- coding: utf-8 -*-
"""对上游 chatgpt-rebind-standalone 原生流水线的薄适配层。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

import settings
from roxy_flow import (
    ProxyFailure,
    RebindOutcomeUnknown,
    ReplacementEmailFailure,
    TaskStopRequested,
)

UPSTREAM_URL = "https://github.com/MervLis/chatgpt-rebind-standalone"
UPSTREAM_COMMIT = "e27b3217dbfddab19e83dc57ab225173877e4663"
UPSTREAM_ROOT = Path(__file__).resolve().parent / "integrations" / "chatgpt_rebind_standalone"

# 上游源码保持原目录、原 import 和原实现；这里只把其项目根加入模块搜索路径。
if str(UPSTREAM_ROOT) not in sys.path:
    sys.path.insert(0, str(UPSTREAM_ROOT))

from rebind_core.pipeline import RebindResult, run_rebind_email  # noqa: E402


def _stop(stop_check: Callable[[], bool]) -> None:
    if stop_check():
        raise TaskStopRequested("用户已请求停止")


def _looks_like_proxy_failure(message: str) -> bool:
    text = str(message or "").lower()
    return any(marker in text for marker in (
        "proxy", "socks", "connection", "connect", "timeout", "timed out",
        "resolve", "network", "tls", "ssl",
    ))


def _raise_upstream_failure(result: RebindResult, new_email: str) -> None:
    code = str(result.code or "REBIND_FAILED")
    message = f"{code}: {result.message}"
    steps = {str(item.get("step") or "") for item in (result.trace or [])}
    if _looks_like_proxy_failure(message):
        raise ProxyFailure(message)
    if code == "MAIL_TIMEOUT":
        raise ReplacementEmailFailure("otp_unavailable", message)
    if code == "BEGIN_FAILED" and any(marker in message.lower() for marker in (
        "already", "exists", "in use", "occupied", "占用", "已使用",
    )):
        raise ReplacementEmailFailure("email_in_use", message)
    if "verify" in steps or code == "RELOGIN_FAILED":
        raise RebindOutcomeUnknown(new_email, message)
    raise RuntimeError(message)


def run_upstream_rebind(
    *,
    old_email: str,
    new_email: str,
    password: str,
    totp_secret: str,
    api_url: str,
    proxy_url: str,
    progress: Callable[[str, str], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> dict:
    """原样调用上游 ``run_rebind_email``，仅把结果接回分站任务模型。"""
    progress = progress or (lambda _stage, _message: None)
    stop_check = stop_check or (lambda: False)
    _stop(stop_check)
    progress("protocol_upstream", "调用 chatgpt-rebind-standalone 原生纯协议流水线")
    result = run_rebind_email(
        old_email=old_email,
        password=password,
        totp_secret=totp_secret,
        new_email=new_email,
        mail_api=api_url,
        proxy=proxy_url or None,
        mail_timeout=float(settings.OTP_MAX_WAIT),
    )
    _stop(stop_check)
    if not result.ok:
        _raise_upstream_failure(result, new_email)

    bundle_path = Path(str(result.bundle_path or ""))
    if not bundle_path.is_file():
        raise RebindOutcomeUnknown(new_email, "上游返回成功，但 login_bundle 文件不存在")
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    session_email = str(bundle.get("email") or result.session_email or new_email).strip()
    access_token = str(bundle.get("access_token") or "").strip()
    if not access_token:
        raise RebindOutcomeUnknown(new_email, "上游返回成功，但 login_bundle 缺少 access_token")

    progress("protocol_verified", "上游原生纯协议换绑、重登和 AT 导出完成")
    return {
        "email": session_email,
        "access_token": access_token,
        "session": bundle.get("auth_session") or {},
        "protocol_engine": "chatgpt-rebind-standalone/run_rebind_email",
        "protocol_upstream_commit": UPSTREAM_COMMIT,
        "protocol_bundle_path": str(bundle_path),
        "roxy_profile_id": "",
        "roxy_browser_status": "not_opened",
    }
