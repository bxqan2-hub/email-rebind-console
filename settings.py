# -*- coding: utf-8 -*-
"""独立换绑站运行配置。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("EMAIL_REBIND_DATA_DIR", str(BASE_DIR / "data"))).resolve()
LOG_DIR = Path(os.getenv("EMAIL_REBIND_LOG_DIR", str(BASE_DIR / "logs"))).resolve()
MAIN_SITE_PATH = Path(
    os.getenv(
        "GPT_MAIN_SITE_PATH",
        str(BASE_DIR.parent / "turb-gpt-free-register"),
    )
).resolve()

HOST = os.getenv("EMAIL_REBIND_HOST", "127.0.0.1")
PORT = int(os.getenv("EMAIL_REBIND_PORT", "5091"))
DEFAULT_WORKERS = max(1, int(os.getenv("EMAIL_REBIND_WORKERS", "2")))
# 可见 Roxy 窗口尺寸；先放大再居中，减少账号信息和检测页面被折叠到滚动区的情况。
ROXY_WINDOW_WIDTH = max(1024, min(int(os.getenv("EMAIL_REBIND_ROXY_WINDOW_WIDTH", "1500")), 2400))
ROXY_WINDOW_HEIGHT = max(720, min(int(os.getenv("EMAIL_REBIND_ROXY_WINDOW_HEIGHT", "950")), 1600))
OTP_MAX_WAIT = max(30, int(os.getenv("EMAIL_REBIND_OTP_MAX_WAIT", "150")))
OTP_POLL_INTERVAL = max(1.0, float(os.getenv("EMAIL_REBIND_OTP_POLL_INTERVAL", "3")))
MAIL_VERIFY_TLS = os.getenv("EMAIL_REBIND_MAIL_VERIFY_TLS", "1").strip().lower() not in {"0", "false", "no"}
MAX_REPLACEMENT_ATTEMPTS = max(1, min(int(os.getenv("EMAIL_REBIND_MAX_REPLACEMENT_ATTEMPTS", "5")), 50))
MAX_PROXY_ATTEMPTS = max(1, min(int(os.getenv("EMAIL_REBIND_MAX_PROXY_ATTEMPTS", "5")), 50))
# 网络、登录页加载、Roxy/ChatGPT 临时响应失败时，保持同一原邮箱和替换邮箱
# 自动重新执行完整流程；验证码已提交或服务端已确认后的不确定结果不会走这里。
MAX_TRANSIENT_RETRIES = max(0, min(int(os.getenv("EMAIL_REBIND_MAX_TRANSIENT_RETRIES", "2")), 10))
TRANSIENT_RETRY_DELAY = max(0.0, min(float(os.getenv("EMAIL_REBIND_TRANSIENT_RETRY_DELAY", "2")), 30.0))
