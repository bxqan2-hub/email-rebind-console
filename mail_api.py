# -*- coding: utf-8 -*-
"""替换邮箱 API 取码：兼容 JSON、纯文本和简单 HTML 读取接口。"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import requests

import settings

_OTP_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_PREFERRED_KEYS = {"code", "otp", "verification_code", "verificationcode", "passcode", "pin"}


def _strings(value: Any, key: str = ""):
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _strings(child, str(child_key))
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child, key)
    elif value is not None:
        yield key.lower(), str(value)


def extract_otp(text: str) -> str | None:
    raw = str(text or "")
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = None
    if decoded is not None:
        values = list(_strings(decoded))
        for key, value in values:
            if key.replace("-", "_") in _PREFERRED_KEYS:
                match = _OTP_RE.search(value)
                if match:
                    return match.group(1)
        contextual = [value for _, value in values if re.search(r"openai|chatgpt|verification|验证码|認証|인증", value, re.I)]
        for value in contextual + [value for _, value in values]:
            match = _OTP_RE.search(value)
            if match:
                return match.group(1)
    matches = _OTP_RE.findall(raw)
    if not matches:
        return None
    for match in matches:
        at = raw.find(match)
        nearby = raw[max(0, at - 140):at + 140]
        if re.search(r"openai|chatgpt|verification|验证码|認証|인증|code|otp", nearby, re.I):
            return match
    return matches[0]


def read_current_otp(api_url: str, email: str, timeout: float = 8.0) -> str | None:
    url = str(api_url or "").replace("{email}", str(email or ""))
    response = requests.get(
        url,
        headers={"Accept": "application/json,text/plain,text/html,*/*", "User-Agent": "email-rebind-console/1.0"},
        timeout=max(2.0, float(timeout or 8.0)),
        verify=settings.MAIL_VERIFY_TLS,
    )
    response.raise_for_status()
    return extract_otp(response.text)


def wait_for_new_otp(
    api_url: str,
    email: str,
    *,
    previous: str | None = None,
    max_wait: int = 150,
    interval: float = 3.0,
    stop_check: Callable[[], bool] | None = None,
) -> str:
    deadline = time.monotonic() + max(15, int(max_wait or 150))
    last_error = ""
    seen_at: dict[str, float] = {}
    while time.monotonic() < deadline:
        if stop_check and stop_check():
            raise RuntimeError("验证码页面已离开，停止取码")
        try:
            code = read_current_otp(api_url, email)
            if code:
                seen_at.setdefault(code, time.monotonic())
                if code != previous:
                    return code
                # 有些服务会为重发邮件复用相同数字；持续出现 20 秒后允许提交一次。
                if time.monotonic() - seen_at[code] >= 20:
                    return code
        except Exception as exc:  # noqa: BLE001 - 保留最后一次网络错误用于终态
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(max(1.0, float(interval or 3.0)))
    raise TimeoutError(f"等待新邮箱验证码超时：{last_error or '取码接口未返回 6 位验证码'}")
