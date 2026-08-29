# -*- coding: utf-8 -*-
"""Reuse the main project's AT plan/trial checker from the rebind console."""
from __future__ import annotations

import sys

import settings


def _load_main_modules():
    root = str(settings.MAIN_SITE_PATH)
    if not settings.MAIN_SITE_PATH.exists():
        raise RuntimeError(f"主站目录不存在：{settings.MAIN_SITE_PATH}")
    if root not in sys.path:
        sys.path.insert(0, root)
    from core import detection_proxy  # type: ignore
    from core.chatgpt_plan import check_account_plan  # type: ignore

    return detection_proxy, check_account_plan


def inspect_detection_proxy(proxy_spec: str) -> dict:
    detection_proxy, _check_account_plan = _load_main_modules()
    return detection_proxy.inspect_static_proxy(str(proxy_spec or "").strip())


def check_zero_trial(access_token: str, proxy_spec: str) -> dict:
    detection_proxy, check_account_plan = _load_main_modules()
    spec = str(proxy_spec or "").strip()
    if not spec:
        raise ValueError("未配置资格检测代理")
    labeled_country = str(detection_proxy.infer_detection_proxy_country(spec) or "").strip().upper()
    if labeled_country:
        inspected = {
            "proxy": detection_proxy.resolve_static_detection_proxy(spec),
            "country": labeled_country,
            "country_source": "proxy_region_tag",
            "masked_proxy": "",
            "exit_ip": "",
            "region": "",
            "city": "",
        }
    else:
        inspected = detection_proxy.inspect_static_proxy(spec)
    resolved_proxy = str(inspected.get("proxy") or "").strip()
    country = str(inspected.get("country") or "").strip().upper()
    result = check_account_plan(
        str(access_token or "").strip(),
        proxy=resolved_proxy,
        timezone_offset_min=detection_proxy.infer_timezone_offset_min(spec, fallback="-"),
        max_attempts=0,
        fast_mode=True,
    )
    offer_kind = str(result.get("plus_trial_offer_kind") or "").strip().lower()
    result.update({
        "trial_zero_trial_eligible": bool(
            result.get("plus_trial_eligible") and offer_kind == "free_trial"
        ),
        "trial_proxy_country": country,
        "trial_proxy_source": str(inspected.get("country_source") or ""),
        "trial_proxy_exit_ip": str(inspected.get("exit_ip") or ""),
        "trial_proxy_region": str(inspected.get("region") or ""),
        "trial_proxy_city": str(inspected.get("city") or ""),
        "trial_proxy_display": str(inspected.get("masked_proxy") or ""),
    })
    return result
