# -*- coding: utf-8 -*-
"""分站对 chatgpt-rebind-standalone 纯协议换绑流程的无落盘适配层。"""
from __future__ import annotations

import time
import uuid
from typing import Callable

import settings
from integrations.chatgpt_rebind_standalone.rebind_core.change_email import (
    ChangeEmailClient,
    ChangeEmailError,
    password_reauth,
)
from integrations.chatgpt_rebind_standalone.rebind_core.mail_inbox import wait_code
from integrations.chatgpt_rebind_standalone.rebind_core.mfa_login import (
    MfaLoginError,
    login_with_password_and_totp,
)
from integrations.chatgpt_rebind_standalone.rebind_core.session_export import build_login_bundle
from roxy_flow import (
    ProxyFailure,
    RebindOutcomeUnknown,
    ReplacementEmailFailure,
    TaskStopRequested,
)

UPSTREAM_URL = "https://github.com/MervLis/chatgpt-rebind-standalone"
UPSTREAM_COMMIT = "e27b3217dbfddab19e83dc57ab225173877e4663"


def _stop(stop_check: Callable[[], bool]) -> None:
    if stop_check():
        raise TaskStopRequested("用户已请求停止")


def _looks_like_proxy_failure(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in (
        "proxy", "socks", "connection", "connect", "timeout", "timed out",
        "resolve", "network", "tls", "ssl",
    ))


def _login(email: str, password: str, totp_secret: str, proxy_url: str):
    try:
        return login_with_password_and_totp(
            email, password, totp_secret, proxy=proxy_url or None,
        )
    except MfaLoginError as exc:
        if _looks_like_proxy_failure(exc):
            raise ProxyFailure(str(exc)) from exc
        raise
    except Exception as exc:
        if _looks_like_proxy_failure(exc):
            raise ProxyFailure(str(exc)) from exc
        raise


def perform_email_rebind(
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
    """执行上游的登录→eligibility→begin→verify→新邮箱重登纯协议链。"""
    progress = progress or (lambda _stage, _message: None)
    stop_check = stop_check or (lambda: False)
    if not str(password or "").strip() or not str(totp_secret or "").strip():
        raise RuntimeError("纯协议换绑只支持带 OpenAI 密码和 2FA Secret 的原账号")
    if not str(proxy_url or "").strip():
        raise ProxyFailure("纯协议换绑没有分配代理")

    _stop(stop_check)
    progress("protocol_login_old", "纯协议登录原邮箱（密码 + TOTP）")
    login_old = _login(old_email, password, totp_secret, proxy_url)

    _stop(stop_check)
    progress("check_email_eligibility", "纯协议检查 change_email eligibility")
    client = ChangeEmailClient(login=login_old)
    client.eligibility()

    _stop(stop_check)
    progress("submit_new_email", "纯协议 begin 发送替换邮箱验证码")
    issued_after = time.time()
    try:
        client.begin(new_email)
    except ChangeEmailError as exc:
        if exc.code == "REAUTH_FAILED":
            progress("protocol_reauth", "begin 要求近期验证，纯协议执行密码 + TOTP reauth")
            password_reauth(login_old)
            client = ChangeEmailClient(login=login_old, session_id=str(uuid.uuid4()))
            client.begin(new_email)
        elif any(marker in str(exc).lower() for marker in (
            "already", "exists", "in use", "occupied", "占用", "已使用",
        )):
            raise ReplacementEmailFailure("email_in_use", str(exc)) from exc
        else:
            raise

    _stop(stop_check)
    progress("wait_new_email_otp", "等待替换邮箱验证码")
    try:
        code = wait_code(
            api_url,
            issued_after=issued_after - 5,
            timeout=float(settings.OTP_MAX_WAIT),
            poll_interval=float(settings.OTP_POLL_INTERVAL),
        )
    except TimeoutError as exc:
        raise ReplacementEmailFailure("otp_unavailable", str(exc)) from exc

    _stop(stop_check)
    progress("submit_new_email_otp", "纯协议 verify 提交替换邮箱验证码")
    try:
        client.verify(new_email, code)
    except Exception as exc:
        raise RebindOutcomeUnknown(new_email, f"verify 后结果待核验：{exc}") from exc

    progress("changed", "纯协议换绑已确认")
    _stop(stop_check)
    progress("protocol_relogin_new", "纯协议使用替换邮箱重新登录并获取 AT")
    try:
        login_new = _login(new_email, password, totp_secret, proxy_url)
    except TaskStopRequested:
        raise
    except Exception as exc:
        raise RebindOutcomeUnknown(new_email, f"换绑已确认，但替换邮箱纯协议重登失败：{exc}") from exc

    bundle = build_login_bundle(login_new, rebind_email=new_email)
    session_email = str(bundle.get("email") or new_email).strip()
    if session_email and session_email.lower() != str(new_email).strip().lower():
        raise RebindOutcomeUnknown(
            new_email,
            f"换绑已确认，但重登邮箱不匹配：期望 {new_email}，实际 {session_email}",
        )
    access_token = str(bundle.get("access_token") or "").strip()
    if not access_token:
        raise RebindOutcomeUnknown(new_email, "换绑已确认，但纯协议重登结果缺少 access_token")
    progress("protocol_verified", "纯协议换绑、新邮箱重登和 AT 校验完成")
    return {
        "email": session_email or new_email,
        "access_token": access_token,
        "session": bundle.get("auth_session") or {},
        "protocol_engine": "chatgpt-rebind-standalone",
        "protocol_upstream_commit": UPSTREAM_COMMIT,
        "roxy_profile_id": "",
        "roxy_browser_status": "not_opened",
    }
