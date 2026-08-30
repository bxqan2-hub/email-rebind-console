# -*- coding: utf-8 -*-
"""纯协议换绑成功后可选的 Roxy 新窗口登录扩展。"""
from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import pyotp

import mail_api
import settings

logger = logging.getLogger(__name__)
_RETAINED_LOCK = threading.RLock()
_RETAINED: dict[str, dict] = {}


class ReplacementEmailFailure(RuntimeError):
    """替换邮箱自身不可用；工作线程应隔离该邮箱并自动轮换下一个。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code or "replacement_failed")


class RebindOutcomeUnknown(RuntimeError):
    """邮箱可能已经换绑，禁止自动换下一个邮箱，必须保留现场供人工核验。"""

    def __init__(self, new_email: str, message: str):
        super().__init__(message)
        self.new_email = str(new_email or "").strip()


class ProxyFailure(RuntimeError):
    """换绑代理在创建 Roxy 环境前检测失败；工作线程应隔离并换下一条。"""


class TaskStopRequested(RuntimeError):
    """用户点击停止后，中断当前协议流程并由 worker 安全释放资源。"""


def _load_main_roxy():
    if not settings.MAIN_SITE_PATH.exists():
        raise RuntimeError(f"未找到主站目录：{settings.MAIN_SITE_PATH}")
    path = str(settings.MAIN_SITE_PATH)
    if path not in sys.path:
        sys.path.insert(0, path)
    from core.roxy_registration import (  # type: ignore
        _build_driver,
        _center_browser_window,
        _fetch_chatgpt_session,
        _safe_get,
        _submit_email_and_wait_next,
    )
    from core.roxybrowser_client import RoxyBrowserClient  # type: ignore
    from core.browser_exit_geo import probe_selenium_driver_exit_geo  # type: ignore

    return (
        RoxyBrowserClient, _build_driver, _center_browser_window, _fetch_chatgpt_session,
        _safe_get, _submit_email_and_wait_next, probe_selenium_driver_exit_geo,
    )


def _retain_browser(*, profile_id: str, email: str, client, opened, driver) -> None:
    key = str(profile_id or "").strip()
    if not key:
        raise RuntimeError("Roxy 成功窗口缺少 profile_id")
    record = {
        "profile_id": key, "email": str(email or "").strip(),
        "client": client, "opened": opened, "driver": driver,
        "lock": threading.RLock(),
    }
    with _RETAINED_LOCK:
        previous = _RETAINED.pop(key, None)
        _RETAINED[key] = record
    if previous and previous.get("driver") is not driver:
        try:
            previous["driver"].quit()
        except Exception:
            pass


def _open_existing_profile(client, profile_id: str):
    """打开已保留的 Roxy 环境，不触发“一号一新环境”的创建限制。"""
    from config import roxybrowser as roxy_cfg  # type: ignore
    from core.roxybrowser_client import RoxyOpenResult, _first, _workspace_id_value  # type: ignore

    key = str(profile_id or "").strip()
    if not key:
        raise RuntimeError("成功账号没有可用的 Roxy profile_id")
    path = str(roxy_cfg.ROXY_OPEN_PATH).format(profile_id=key)
    body = dict(getattr(roxy_cfg, "ROXY_OPEN_EXTRA_PARAMS", {}) or {})
    body.setdefault("workspaceId", _workspace_id_value())
    body.setdefault("dirId", int(key) if key.isdigit() else key)
    body.setdefault("args", [])
    body.setdefault("forceOpen", True)
    body["headless"] = False
    method = str(roxy_cfg.ROXY_OPEN_METHOD or "POST").upper()
    result = client.request(
        method, path,
        params=body if method == "GET" else None,
        json_body=body if method != "GET" else None,
    )
    debugger_address = client._extract_debugger_address(result)
    webdriver_url = _first(result, [
        ("webdriver",), ("webDriver",), ("webdriver_url",), ("webdriverUrl",),
        ("selenium",), ("selenium_url",), ("seleniumUrl",),
        ("data", "webdriver"), ("data", "webDriver"),
        ("data", "webdriver_url"), ("data", "webdriverUrl"),
        ("data", "selenium"), ("data", "selenium_url"), ("data", "seleniumUrl"),
    ]) or None
    ws_endpoint = _first(result, [
        ("ws",), ("wsEndpoint",), ("ws_endpoint",), ("debuggerWsUrl",),
        ("data", "ws"), ("data", "wsEndpoint"),
        ("data", "ws_endpoint"), ("data", "debuggerWsUrl"),
    ]) or None
    if not debugger_address and not webdriver_url:
        raise RuntimeError("Roxy 已打开保留环境，但没有返回 Selenium/调试地址")
    return RoxyOpenResult(
        key, result, debugger_address=debugger_address,
        webdriver_url=webdriver_url, ws_endpoint=ws_endpoint,
        created_by_run=False,
    )


def _roxy_cdp_port(opened) -> int | None:
    """从 Roxy 打开结果中提取可供抓包工具连接的 CDP 端口。"""
    for value in (
        getattr(opened, "debugger_address", None),
        getattr(opened, "ws_endpoint", None),
    ):
        text = str(value or "").strip()
        if not text:
            continue
        try:
            parsed = urlsplit(text if "://" in text else f"//{text}")
            if parsed.port and 0 < parsed.port <= 65535:
                return int(parsed.port)
        except ValueError:
            continue
    return None


def resolve_roxy_cdp_port(profile_id: str) -> int | None:
    """读取当前成功环境的 CDP 端口；重启后会重新打开环境并回填。"""
    key = str(profile_id or "").strip()
    if not key:
        return None
    with _RETAINED_LOCK:
        retained = _RETAINED.get(key)
    if retained:
        port = _roxy_cdp_port(retained.get("opened"))
        if port:
            return port
    RoxyBrowserClient = _load_main_roxy()[0]
    opened = _open_existing_profile(RoxyBrowserClient(), key)
    return _roxy_cdp_port(opened)


def _retained_or_reopen(profile_id: str, expected_email: str) -> dict:
    key = str(profile_id or "").strip()
    with _RETAINED_LOCK:
        existing = _RETAINED.get(key)
    if existing:
        return existing
    RoxyBrowserClient, build_driver, center_window, *_rest = _load_main_roxy()
    client = RoxyBrowserClient()
    opened = _open_existing_profile(client, key)
    driver = build_driver(opened)
    center_window(driver)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)
    _retain_browser(
        profile_id=key, email=expected_email, client=client,
        opened=opened, driver=driver,
    )
    with _RETAINED_LOCK:
        return _RETAINED[key]


def refresh_retained_access_token(profile_id: str, expected_email: str) -> dict:
    """复用成功窗口的登录态重新读取 session/AT。"""
    record = _retained_or_reopen(profile_id, expected_email)
    with record["lock"]:
        _client_type, _build, _center, fetch_session, _safe_get, _submit_email, _probe_driver_exit = _load_main_roxy()
        driver = record["driver"]
        # Force the browser to reload ChatGPT before reading NextAuth session;
        # a cached document can otherwise hand back the previously stored,
        # revoked access token and make refresh appear successful.
        try:
            driver.refresh()
            time.sleep(2)
        except Exception:
            logger.debug("Roxy refresh 页面刷新失败，继续读取 session", exc_info=True)
        session = fetch_session(
            driver, timeout=45, auto_jump_wait=3, refresh_attempts=1,
        )
        observed = _session_email(session)
        expected = str(expected_email or "").strip()
        if expected and observed.lower() != expected.lower():
            raise RuntimeError(f"保留窗口登录邮箱不匹配：期望 {expected}，实际 {observed or '空'}")
        access_token = str(session.get("accessToken") or "").strip()
        if not access_token:
            raise RuntimeError("保留窗口 /api/auth/session 没有返回 accessToken")
        record["email"] = observed or expected
        return {
            "email": observed or expected, "access_token": access_token,
            "session": session, "roxy_profile_id": str(profile_id),
            "roxy_cdp_port": _roxy_cdp_port(record.get("opened")),
        }


def delete_retained_profile(profile_id: str) -> bool:
    """关闭成功账号窗口并删除 Roxy Profile，避免环境持续堆积。"""
    key = str(profile_id or "").strip()
    if not key:
        raise RuntimeError("成功账号没有可关闭的 Roxy profile_id")
    with _RETAINED_LOCK:
        record = _RETAINED.get(key)
    client = record["client"] if record else _load_main_roxy()[0]()
    lock = record["lock"] if record else threading.RLock()
    with lock:
        closed = bool(client.close_profile(key))
        if record:
            try:
                record["driver"].quit()
            except Exception:
                pass
        deleted = bool(client.delete_profile(key))
    with _RETAINED_LOCK:
        if record and _RETAINED.get(key) is record:
            _RETAINED.pop(key, None)
    if not deleted:
        raise RuntimeError(
            f"Roxy Profile 删除失败：profile={key}, closed={closed}；请重试关闭并删除"
        )
    return True


def retained_profile_is_connected(profile_id: str) -> bool:
    with _RETAINED_LOCK:
        return str(profile_id or "").strip() in _RETAINED


def _body_text(driver) -> str:
    try:
        return str(driver.execute_script("return (document.body && document.body.innerText) || ''") or "")
    except Exception:
        return ""


def _visible_input(driver, selectors: list[str]):
    script = r"""
    const selectors = arguments[0];
    const visible = el => !!el && !el.disabled && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
    for (const selector of selectors) {
      for (const el of document.querySelectorAll(selector)) if (visible(el)) return el;
    }
    return null;
    """
    return driver.execute_script(script, selectors)


def _submit_login_email_allow_password(
    driver,
    email: str,
    password: str,
    totp_secret: str,
    submit_email,
    *,
    attempts: int = 2,
) -> str:
    """提交登录邮箱；密码+2FA账号允许停在登录密码页交给本地登录器。

    主站的通用邮箱提交器默认把 ``login_password`` 判为注册账号不可用，
    这对换绑站的“邮箱----密码----2FA”原账号是不对的：该页面正是下一步
    应该填写密码和 TOTP 的登录流程。仅当两项凭据都已导入且页面/异常明确
    表示登录密码页时放行；邮箱 API 账号仍保持原有失败行为。
    """
    try:
        return str(submit_email(driver, email, attempts=attempts) or "")
    except Exception as exc:
        if not (str(password or "").strip() and str(totp_secret or "").strip()):
            raise
        current = str(getattr(driver, "current_url", "") or "").lower()
        message = str(exc or "")
        is_password_page = (
            "auth.openai.com/log-in/password" in current
            or "login_password" in message.lower()
            or "existing-account password page" in message.lower()
            or "登录密码页" in message
        )
        if not is_password_page:
            raise
        logger.info("%s 已识别为密码+2FA原账号，允许进入登录密码页：%s", email, current or message[:180])
        return "login_password"


def _set_value(driver, element, value: str) -> None:
    driver.execute_script(r"""
    const el = arguments[0], value = arguments[1];
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, value); else el.value = value;
    el.dispatchEvent(new Event('input', {bubbles:true}));
    el.dispatchEvent(new Event('change', {bubbles:true}));
    el.focus();
    """, element, value)


def _set_otp_value(driver, element, value: str) -> None:
    """兼容单框 OTP 和实验中的 4–8 个逐位输入框。"""
    code = str(value or "").strip()
    filled = driver.execute_script(r"""
    const anchor = arguments[0], value = String(arguments[1] || '');
    const root = anchor?.closest('[data-testid="modal-add-email-otp"], [role="dialog"], form') || document;
    const visible = el => !!el && !el.disabled && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)
      && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
    const boxes = [...root.querySelectorAll('input')].filter(el => visible(el) && Number(el.maxLength) === 1);
    if (boxes.length < 4 || boxes.length > 8 || value.length > boxes.length) return 0;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    boxes.forEach((el, index) => {
      const digit = value[index] || '';
      if (setter) setter.call(el, digit); else el.value = digit;
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
    });
    (boxes[Math.min(value.length, boxes.length) - 1] || boxes[0]).focus();
    return boxes.length;
    """, element, code)
    if not filled:
        _set_value(driver, element, code)


def _submit_near(driver, element) -> None:
    result = driver.execute_script(r"""
    const el = arguments[0];
    const form = el.closest('form');
    const buttons = [...(form || document).querySelectorAll('button, input[type="submit"]')]
      .filter(x => !x.disabled && (x.offsetWidth || x.offsetHeight || x.getClientRects().length));
    const preferred = buttons.find(x => (x.type || '').toLowerCase() === 'submit') || buttons[buttons.length - 1];
    if (preferred) { preferred.click(); return true; }
    if (form?.requestSubmit) { form.requestSubmit(); return true; }
    return false;
    """, element)
    if not result:
        element.send_keys("\n")


def _session_email(session: dict) -> str:
    user = session.get("user") if isinstance(session, dict) else {}
    return str((user or {}).get("email") or session.get("email") or "").strip()


def _complete_login(
    driver,
    email: str,
    password: str,
    totp_secret: str,
    fetch_session,
    progress: Callable[[str, str], None],
    *,
    email_api_url: str = "",
    previous_email_otp: str | None = None,
    email_label: str = "邮箱",
    stop_check: Callable[[], bool] | None = None,
) -> dict:
    stop_check = stop_check or (lambda: False)
    deadline = time.monotonic() + 120
    password_sent = False
    totp_sent_at = 0.0
    totp_submitted = False
    email_otp_sent = False
    otp_hint_since = 0.0
    while time.monotonic() < deadline:
        if stop_check():
            raise TaskStopRequested("用户已请求停止")
        current = str(getattr(driver, "current_url", "") or "")
        if "chatgpt.com" in current:
            try:
                session = fetch_session(driver, timeout=12, auto_jump_wait=2, refresh_attempts=0)
                if session.get("accessToken"):
                    return session
            except Exception:
                pass
        password_input = _visible_input(driver, ['input[type="password"]', 'input[name="password"]'])
        if password_input and not password_sent:
            if not str(password or "").strip():
                raise RuntimeError(f"{email_label}登录要求密码，但导入的原邮箱记录只有 API 取码地址")
            progress("login_password", "填写账号密码")
            _set_value(driver, password_input, password)
            _submit_near(driver, password_input)
            password_sent = True
            time.sleep(2)
            continue
        text = _body_text(driver).lower()
        explicit_totp_input = _visible_input(driver, [
            'input[name*="totp" i]', 'input[id*="totp" i]',
            'input[name*="authenticator" i]', 'input[id*="authenticator" i]',
        ])
        code_input = explicit_totp_input or _visible_input(driver, [
            'input[name="verification_code"]',
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]', 'input[name="code"]', 'input[type="tel"]',
            'input[id*="verification" i]', 'input[name*="verification" i]',
            'input[aria-label*="code" i]', 'input[data-testid*="code" i]',
        ])
        totp_page = any(marker in text for marker in (
            "authenticator", "two-factor", "2fa", "verification app", "身份验证器", "动态验证码", "인증 앱",
        ))
        if (
            code_input
            and str(totp_secret or "").strip()
            and not email_otp_sent
        ):
            # Auth pages sometimes expose only a generic verification_code
            # input after the password step. For password+2FA accounts this
            # is the TOTP challenge; do not consume the mailbox API first.
            totp_page = True
        if code_input and totp_page and time.monotonic() - totp_sent_at > 8:
            if not str(totp_secret or "").strip():
                raise RuntimeError(f"{email_label}登录要求 2FA，但导入的原邮箱记录没有 MFA Secret")
            progress("login_totp", "提交原账号 2FA 动态码")
            code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
            _set_otp_value(driver, code_input, code)
            _submit_near(driver, code_input)
            totp_sent_at = time.monotonic()
            totp_submitted = True
            time.sleep(2)
            continue
        if code_input and str(totp_secret or "").strip() and not email_otp_sent:
            # Keep a password+2FA account on the TOTP path across rerenders.
            time.sleep(1)
            continue
        # 新版 Auth 页面有时只渲染数字输入框，正文没有稳定的英文提示；
        # 只要不是 2FA 页面且存在 OTP 语义输入框，就交给对应邮箱 API 取码。
        email_otp_page = bool(code_input and not totp_page)
        otp_page_hint = (
            not totp_page
            and any(marker in text for marker in (
                "verification", "one-time", "enter the code", "验证码", "認証コード", "인증 코드",
            ))
        )
        if otp_page_hint and not code_input:
            if not otp_hint_since:
                otp_hint_since = time.monotonic()
                logger.warning("%s登录已进入验证码页面，但尚未找到可填写输入框", email_label)
            elif time.monotonic() - otp_hint_since >= 15:
                raise RuntimeError(f"{email_label}验证码页面未找到可填写输入框")
        elif code_input:
            otp_hint_since = 0.0
        if email_otp_page and not email_otp_sent:
            if not str(email_api_url or "").strip():
                raise RuntimeError(f"{email_label}登录要求邮箱验证码，但没有对应的 API 取码地址")
            logger.info("%s登录验证码输入框已识别，调用邮箱 API 取码", email_label)
            progress("login_email_otp", f"通过{email_label} API 等待登录验证码")
            try:
                code = mail_api.wait_for_new_otp(
                    email_api_url, email, previous=previous_email_otp,
                    max_wait=settings.OTP_MAX_WAIT, interval=settings.OTP_POLL_INTERVAL,
                    stop_check=lambda: stop_check() or _visible_input(driver, [
                        'input[name="verification_code"]', 'input[autocomplete="one-time-code"]',
                        'input[inputmode="numeric"]', 'input[name="code"]', 'input[type="tel"]',
                        'input[id*="verification" i]', 'input[name*="verification" i]',
                        'input[aria-label*="code" i]',
                    ]) is None,
                )
            except TaskStopRequested:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"{email_label}登录验证码获取失败：{type(exc).__name__}: {str(exc)[:300]}"
                ) from exc
            progress("submit_login_email_otp", f"提交{email_label}登录验证码")
            _set_otp_value(driver, code_input, code)
            _submit_near(driver, code_input)
            email_otp_sent = True
            logger.info("%s登录验证码已提交，等待 /api/auth/session", email_label)
            time.sleep(2)
            continue
        errors = [line.strip() for line in _body_text(driver).splitlines() if "error" in line.lower() or "错误" in line]
        if errors and password_sent:
            logger.debug("login page messages for %s: %s", email, errors[:3])
        time.sleep(1)
    if email_otp_sent:
        raise TimeoutError(f"{email_label}验证码已提交但未取得 ChatGPT accessToken")
    raise TimeoutError(f"{email_label}登录超时；未取得 ChatGPT accessToken")


def _clear_login_state(driver) -> None:
    for origin in ("https://chatgpt.com", "https://auth.openai.com"):
        try:
            driver.execute_cdp_cmd("Storage.clearDataForOrigin", {"origin": origin, "storageTypes": "all"})
        except Exception:
            pass
    try:
        driver.delete_all_cookies()
    except Exception:
        pass


def _login_with_replacement_email(
    *,
    driver,
    email: str,
    password: str,
    totp_secret: str,
    fetch_session,
    safe_get,
    submit_email,
    progress: Callable[[str, str], None],
    api_url: str,
    auth_method: str = "",
    max_relogin_retries: int | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> tuple[dict, str, str]:
    """换绑提交后只用新邮箱 API 登录；验证码/会话失败时重新走登录页。"""
    retries = settings.MAX_TRANSIENT_RETRIES if max_relogin_retries is None else max_relogin_retries
    total_attempts = max(1, min(int(retries or 0), 10) + 1)
    last_error: Exception | None = None
    stop_check = stop_check or (lambda: False)
    password_totp_login = auth_method == "password_totp" or bool(
        str(password or "").strip() and str(totp_secret or "").strip()
    )
    email_api_url = "" if password_totp_login else str(api_url or "").strip()
    for login_attempt in range(1, total_attempts + 1):
        if stop_check():
            raise TaskStopRequested("用户已请求停止")
        progress(
            "relogin_new",
            f"第 {login_attempt}/{total_attempts} 次使用替换邮箱重新登录：{email}",
        )
        previous_otp = None
        if email_api_url:
            try:
                previous_otp = mail_api.read_current_otp(email_api_url, email, timeout=4)
            except Exception:
                # 取码接口的瞬时错误由 wait_for_new_otp 在验证码页继续轮询并给出诊断。
                pass
        _clear_login_state(driver)
        safe_get(
            driver,
            "https://chatgpt.com/auth/login",
            timeout=45,
            attempts=2,
            accept_hosts=("chatgpt.com", "auth.openai.com"),
        )
        _submit_login_email_allow_password(
            driver, email, password, totp_secret, submit_email, attempts=2,
        )
        try:
            session = _complete_login(
                driver,
                email,
                password,
                totp_secret,
                fetch_session,
                progress,
                email_api_url=email_api_url,
                previous_email_otp=previous_otp,
                email_label="替换邮箱",
                stop_check=stop_check,
            )
            observed = _session_email(session)
            if (observed or "").lower() != email.lower():
                raise RuntimeError(f"换绑后登录邮箱校验失败：期望 {email}，实际 {observed or '空'}")
            access_token = str(session.get("accessToken") or "").strip()
            if not access_token:
                raise RuntimeError("换绑后重新登录成功，但 /api/auth/session 未返回 accessToken")
            return session, observed, access_token
        except Exception as exc:
            if isinstance(exc, TaskStopRequested):
                raise
            last_error = exc
            if login_attempt >= total_attempts:
                break
            progress(
                "relogin_new_retry",
                f"第 {login_attempt} 次替换邮箱登录失败，将重新打开登录页获取新验证码：{type(exc).__name__}: {str(exc)[:220]}",
            )
            if settings.TRANSIENT_RETRY_DELAY:
                deadline = time.monotonic() + settings.TRANSIENT_RETRY_DELAY
                while time.monotonic() < deadline:
                    if stop_check():
                        raise TaskStopRequested("用户已请求停止")
                    time.sleep(min(0.25, deadline - time.monotonic()))
    assert last_error is not None
    raise last_error


def perform_replacement_login(
    *,
    new_email: str,
    password: str,
    totp_secret: str,
    api_url: str,
    auth_method: str = "",
    proxy_url: str,
    progress: Callable[[str, str], None] | None = None,
    proxy_verified: Callable[[dict], None] | None = None,
    max_relogin_retries: int | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> dict:
    """已确认换绑后的补救流程：只登录替换邮箱并保留成功窗口。"""
    progress = progress or (lambda _stage, _message: None)
    stop_check = stop_check or (lambda: False)
    RoxyBrowserClient, build_driver, center_window, fetch_session, safe_get, submit_email, probe_driver_exit = _load_main_roxy()
    clean_proxy = str(proxy_url or "").strip()
    if not clean_proxy:
        raise ProxyFailure("任务没有分配换绑代理，已阻止 Roxy 直连")
    client = RoxyBrowserClient(profile_proxy=clean_proxy)
    opened = None
    driver = None
    keep_success_open = False
    try:
        progress("check_proxy", "检测补救登录代理出口；失败时自动切换下一条")
        try:
            opened = client.open_profile(require_proxy_exit_ip=True)
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:400]}"
            if any(marker in message.lower() for marker in ("代理出口", "代理格式", "代理协议", "proxy")):
                raise ProxyFailure(message) from exc
            raise
        exit_geo = dict(getattr(opened, "preflight_exit_geo", {}) or {})
        progress("open_roxy", f"补救登录窗口代理预检通过（出口 {exit_geo.get('ip') or '已确认'}）")
        driver = build_driver(opened)
        center_window(driver)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(60)
        progress("check_proxy", "从补救登录窗口复核实际代理出口")
        browser_exit_geo = probe_driver_exit(
            driver, label="换绑后登录", restore_page_load_timeout=60,
            restore_script_timeout=60, attempts=1, retry_delay=0.5,
        )
        if not browser_exit_geo.get("ip"):
            raise ProxyFailure("补救登录窗口代理出口复核失败")
        exit_geo = dict(browser_exit_geo)
        if callable(proxy_verified):
            proxy_verified(exit_geo)
        progress("open_roxy", f"补救登录窗口代理检测通过（出口 {exit_geo.get('ip')}）")
        try:
            new_session, observed, access_token = _login_with_replacement_email(
                driver=driver,
                email=new_email,
                password=password,
                totp_secret=totp_secret,
                fetch_session=fetch_session,
                safe_get=safe_get,
                submit_email=submit_email,
                progress=progress,
                api_url=api_url,
                auth_method=auth_method,
                max_relogin_retries=max_relogin_retries,
                stop_check=stop_check,
            )
        except TaskStopRequested:
            raise
        except RebindOutcomeUnknown:
            raise
        except Exception as exc:
            raise RebindOutcomeUnknown(
                new_email,
                f"已换绑邮箱重新登录/AT 刷新失败：{type(exc).__name__}: {str(exc)[:300]}",
            ) from exc
        progress("verified", "已换绑邮箱登录和 accessToken 校验完成")
        _retain_browser(
            profile_id=opened.profile_id, email=observed,
            client=client, opened=opened, driver=driver,
        )
        keep_success_open = True
        progress("kept_open", "AT 已获取；补救登录窗口保持替换邮箱登录态")
        return {
            "email": observed, "access_token": access_token, "session": new_session,
            "roxy_profile_id": opened.profile_id, "proxy_exit_geo": exit_geo,
            "roxy_cdp_port": _roxy_cdp_port(opened),
            "roxy_browser_status": "open",
        }
    finally:
        if opened is not None and not keep_success_open:
            logger.info(
                "补救登录未完成，关闭并删除 Roxy 临时环境：profile=%s",
                getattr(opened, "profile_id", ""),
            )
        if driver is not None and not keep_success_open:
            try:
                driver.quit()
            except Exception:
                pass
        if opened is not None and not keep_success_open:
            try:
                client.close_profile(str(opened.profile_id))
            except Exception:
                pass
            if bool(getattr(opened, "created_by_run", True)):
                try:
                    client.delete_profile(str(opened.profile_id))
                except Exception:
                    pass
