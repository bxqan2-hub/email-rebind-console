from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

OTP_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
HTML_CODE_RE = re.compile(
    r"<[^>]*class=[\"'][^\"']*\bcode\b[^\"']*[\"'][^>]*>\s*"
    r"(\d{6})\s*</[^>]+>",
    re.IGNORECASE | re.DOTALL,
)
HTML_SUBJECT_RE = re.compile(
    r"(?:openai|chatgpt)[^<]{0,120}?(?:login\s+code|verification\s+code|code)"
    r"\s*(?:is|:)?\s*(\d{6})",
    re.IGNORECASE | re.DOTALL,
)


def _parse_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # ms or s
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return ts
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _parse_time(int(text))
    try:
        # support Z
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def _extract_candidates(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "mail", "mails", "items", "result", "messages", "list"):
        val = payload.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            # single mail object
            return [val]
    # itself may be a mail object
    return [payload]


def extract_otp_from_text(text: str) -> str:
    """Extract the newest visible OTP without selecting CSS/URL numbers.

    Mailbox endpoints commonly return an HTML list ordered newest-first.  The
    old implementation selected the final six-digit match, which picked the
    oldest message (and, in some pages, a number from CSS or a URL).  Prefer
    explicit code nodes and then visible OpenAI/ChatGPT subject text; both
    preserve the mailbox's newest-first order.
    """
    raw = str(text or "")
    for pattern in (HTML_CODE_RE, HTML_SUBJECT_RE):
        match = pattern.search(raw)
        if match:
            return match.group(1)

    visible = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", raw)
    visible = re.sub(r"<[^>]+>", " ", visible)
    matches = OTP_RE.findall(visible)
    return matches[0] if matches else ""


def fetch_latest_otp(mail_api: str, *, issued_after: float | None = None, timeout: float = 20.0) -> str:
    """GET 收信 API，返回最新可用 6 位码。"""
    url = str(mail_api or "").strip()
    if not url:
        raise ValueError("mail_api 为空")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"mail_api 非法: {url}")

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "").lower()
    raw_text = resp.text or ""
    payload: Any
    if "json" in ctype or raw_text.lstrip().startswith(("{", "[")):
        try:
            payload = resp.json()
        except Exception:
            payload = raw_text
    else:
        payload = raw_text

    if isinstance(payload, (dict, list)):
        for item in _extract_candidates(payload):
            ts = None
            for k in ("time", "timestamp", "created_at", "date", "received_at", "ts"):
                if k in item:
                    ts = _parse_time(item.get(k))
                    if ts is not None:
                        break
            if issued_after is not None and ts is not None and ts + 1 < issued_after:
                continue
            blob = " ".join(
                str(item.get(k) or "")
                for k in ("code", "otp", "subject", "title", "content", "body", "text", "html", "message")
            )
            if not blob.strip():
                blob = str(item)
            code = extract_otp_from_text(blob)
            if code:
                return code
        # fallback whole json text
        code = extract_otp_from_text(raw_text)
        if code:
            return code
        return ""

    return extract_otp_from_text(str(payload))


def wait_code(
    mail_api: str,
    *,
    issued_after: float | None = None,
    timeout: float = 120.0,
    poll_interval: float = 2.5,
) -> str:
    """轮询收信 API，直到拿到 6 位码或超时。"""
    deadline = time.time() + max(5.0, float(timeout))
    last_err = ""
    seen: set[str] = set()
    while time.time() < deadline:
        try:
            code = fetch_latest_otp(mail_api, issued_after=issued_after)
            if code and code not in seen:
                # 若 API 不提供时间戳，至少确保码在轮询窗口内新出现；
                # 第一次看到也接受。
                return code
            if code:
                seen.add(code)
        except Exception as exc:
            last_err = str(exc)
        time.sleep(max(0.5, float(poll_interval)))
    raise TimeoutError(
        f"MAIL_TIMEOUT: {int(timeout)}s 内未从收信 API 取到验证码"
        + (f" ({last_err})" if last_err else "")
    )
