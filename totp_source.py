# -*- coding: utf-8 -*-
"""Normalize imported TOTP secrets and URLs that embed a TOTP secret."""
from __future__ import annotations

import base64
import re
from urllib.parse import parse_qs, unquote, urlsplit

_BASE32_RE = re.compile(r"^[A-Z2-7]+=*$", re.IGNORECASE)


def _validated_secret(value: str) -> str:
    secret = re.sub(r"[\s-]+", "", unquote(str(value or ""))).upper().rstrip("=")
    if len(secret) < 16 or not _BASE32_RE.fullmatch(secret):
        raise ValueError("2FA 内容不是有效的 Base32 Secret")
    try:
        base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    except Exception as exc:
        raise ValueError("2FA 内容不是有效的 Base32 Secret") from exc
    return secret


def resolve_totp_secret(value: str) -> str:
    """Return a Base32 secret from a raw secret, otpauth URI, or 2FA URL.

    Supported HTTP URL forms include a Base32 final path segment (the format
    used by 2fa.fb.tools) and common ``secret`` query parameters.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("未配置 2FA Secret 或 2FA URL")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() in {"http", "https", "otpauth"}:
        query = parse_qs(parsed.query)
        for key in ("secret", "totp_secret", "key"):
            values = query.get(key) or []
            if values:
                return _validated_secret(values[0])
        for segment in reversed([part for part in parsed.path.split("/") if part]):
            try:
                return _validated_secret(segment)
            except ValueError:
                continue
        raise ValueError("2FA URL 中没有可识别的 Base32 Secret")
    return _validated_secret(raw)
