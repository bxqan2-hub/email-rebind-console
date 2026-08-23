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
_HTML_CODE_RE = re.compile(
    r"<[^>]*class=[\"'][^\"']*\bcode\b[^\"']*[\"'][^>]*>\s*(\d{6})\s*</",
    re.I,
)
_HTML_SUBJECT_CODE_RE = re.compile(
    r"(?:chatgpt|openai)[^<]{0,100}?(?:login\s+code|code)\s*(?:is|:)?\s*(\d{6})",
    re.I,
)
_EMBEDDED_HTML_RE = re.compile(
    r"\b(?:htmlContent|emailHtml|messageHtml|mailHtml)\s*=\s*\"((?:\\.|[^\"\\])*)\"",
    re.I | re.S,
)


def _strings(value: Any, key: str = ""):
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _strings(child, str(child_key))
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _strings(child, key)
    elif value is not None:
        yield key.lower(), str(value)


def _decode_embedded_html(raw: str):
    """解码邮件服务把正文放进 JS 字符串的页面。"""
    for match in _EMBEDDED_HTML_RE.finditer(str(raw or "")):
        encoded = match.group(1)
        try:
            decoded = json.loads(f'"{encoded}"')
        except (TypeError, ValueError, json.JSONDecodeError):
            try:
                decoded = bytes(encoded, "utf-8").decode("unicode_escape")
            except (UnicodeDecodeError, ValueError):
                continue
        if "<" in decoded:
            yield decoded


def _extract_html_otp(raw: str) -> str | None:
    """从一个完整 HTML 邮件正文中提取验证码，先排除 CSS/脚本数字。"""
    html_code = _HTML_CODE_RE.search(raw)
    if html_code:
        return html_code.group(1)
    html_subject = _HTML_SUBJECT_CODE_RE.search(re.sub(r"<[^>]+>", " ", raw))
    if html_subject:
        return html_subject.group(1)
    visible = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", raw)
    visible = re.sub(r"<[^>]+>", " ", visible)
    matches = _OTP_RE.findall(visible)
    if not matches:
        return None
    for match in matches:
        at = visible.find(match)
        nearby = visible[max(0, at - 180):at + 180]
        if re.search(r"openai|chatgpt|verification|验证码|認証|인증|code|otp", nearby, re.I):
            return match
    return matches[0]


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
    # 部分取码服务把真实邮件正文放在 script 的 htmlContent JS 字符串中，
    # 外层页面本身只有标题和 iframe；必须先解码内层正文再识别验证码。
    for embedded in _decode_embedded_html(raw):
        code = _extract_html_otp(embedded)
        if code:
            return code
    # 邮箱取码服务返回 HTML 时，先读邮件卡片的显式 code 节点/主题。
    # 不能直接扫描整页：CSS 色值、UUID 和链接路径也会产生 6 位数字，
    # 恰好靠近 ChatGPT 文本时会被误判成验证码。
    return _extract_html_otp(raw)


def read_current_otp(api_url: str, email: str, timeout: float = 8.0) -> str | None:
    url = str(api_url or "").replace("{email}", str(email or ""))
    response = requests.get(
        url,
        headers={
            "Accept": "application/json,text/plain,text/html,*/*",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "email-rebind-console/1.0",
        },
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
