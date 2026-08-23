# -*- coding: utf-8 -*-
"""通过 Roxy 指纹浏览器执行 ChatGPT 邮箱换绑 API 协议。"""
from __future__ import annotations

import base64
import json
import logging
import sys
import threading
import time
from typing import Callable

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


class _HarApiUnavailable(RuntimeError):
    """抓包确认的换绑 API 在当前页面版本不可用；由工作线程关闭窗口并重试。"""


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


def _session_account_id(session: dict) -> str:
    """从当前 session 的 accessToken 中只读提取 ChatGPT workspace account id。"""
    if not isinstance(session, dict):
        return ""
    direct = str(session.get("accountId") or session.get("account_id") or "").strip()
    if direct:
        return direct
    token = str(session.get("accessToken") or "").strip()
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    auth = payload.get("https://api.openai.com/auth")
    auth = auth if isinstance(auth, dict) else {}
    return str(auth.get("chatgpt_account_id") or payload.get("account_id") or "").strip()


def _browser_json_request(
    driver,
    method: str,
    path: str,
    *,
    account_id: str = "",
    access_token: str = "",
    body: dict | None = None,
    timeout: float = 45.0,
) -> dict:
    """在已登录的 ChatGPT 页面上下文发送同源 JSON 请求，不导出 Cookie/token。"""
    script = r"""
    const method = String(arguments[0] || 'GET').toUpperCase();
    const path = String(arguments[1] || '');
    const accountId = String(arguments[2] || '');
    const accessToken = String(arguments[3] || '');
    const body = arguments[4];
    const timeoutMs = Math.max(1000, Number(arguments[5]) || 45000);
    const done = arguments[6];
    const controller = new AbortController();
    let finished = false;
    const finish = value => {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      done(value);
    };
    const timer = setTimeout(() => {
      controller.abort();
      finish({ok:false, status:0, error:'request_timeout'});
    }, timeoutMs);
    const headers = {'accept':'application/json'};
    if (accountId) headers['chatgpt-account-id'] = accountId;
    if (accessToken) headers['authorization'] = `Bearer ${accessToken}`;
    if (body !== null && body !== undefined) headers['content-type'] = 'application/json';
    fetch(path, {
      method,
      credentials:'include',
      cache:'no-store',
      headers,
      body: body === null || body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    }).then(async response => {
      const text = await response.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch (_) {}
      finish({ok:true, status:response.status, data, textLength:text.length});
    }).catch(error => finish({ok:false, status:0, error:String(error?.name || 'fetch_error')}));
    """
    try:
        result = driver.execute_async_script(
            script,
            str(method or "GET"),
            str(path or ""),
            str(account_id or ""),
            str(access_token or ""),
            body,
            max(1.0, float(timeout or 45.0)) * 1000,
        )
    except Exception as exc:
        return {"ok": False, "status": 0, "error": type(exc).__name__}
    return dict(result or {})


def _eligibility_allowed(data) -> bool:
    if not isinstance(data, dict):
        return False
    for key in ("eligible", "is_eligible", "isEligible", "can_change_email", "canChangeEmail"):
        if key in data:
            return data.get(key) is True
    return False


def _api_error_code(data) -> str:
    if not isinstance(data, dict):
        return ""
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
    detail_error = detail.get("error") if isinstance(detail.get("error"), dict) else {}
    for candidate in (
        data.get("code"), data.get("error_code"), error.get("code"),
        detail.get("code"), detail.get("error_code"), detail_error.get("code"),
    ):
        value = str(candidate or "").strip().lower()
        if value and len(value) <= 80:
            return value
    return ""


def _change_email_via_har_api(
    driver,
    *,
    session: dict,
    new_email: str,
    api_url: str,
    progress: Callable[[str, str], None],
    stop_check: Callable[[], bool] | None = None,
) -> None:
    """按已验证 HAR 时序执行 eligibility → begin → verify → logout。"""
    account_id = _session_account_id(session)
    access_token = str((session or {}).get("accessToken") or "").strip()
    if not account_id:
        raise _HarApiUnavailable("登录 session 中没有 chatgpt_account_id")
    if not access_token:
        raise _HarApiUnavailable("登录 session 中没有 accessToken")
    stop_check = stop_check or (lambda: False)
    if stop_check():
        raise TaskStopRequested("用户已请求停止")

    progress("check_email_eligibility", "检查当前账号的邮箱换绑资格")
    eligibility = _browser_json_request(
        driver, "GET", "/backend-api/accounts/change_email/eligibility",
        account_id=account_id, access_token=access_token,
    )
    if not eligibility.get("ok") or int(eligibility.get("status") or 0) in {404, 405}:
        raise _HarApiUnavailable("换绑资格接口不可用")
    status = int(eligibility.get("status") or 0)
    if status in {401, 403}:
        raise _HarApiUnavailable("换绑资格接口要求页面登录态")
    if status != 200:
        raise RuntimeError(f"检查换绑资格失败：HTTP {status}")
    eligibility_data = eligibility.get("data") if isinstance(eligibility.get("data"), dict) else {}
    if not _eligibility_allowed(eligibility_data):
        raise RuntimeError("当前账号不符合自助换绑条件")
    eligibility_type = str(eligibility_data.get("eligibility_type") or "").strip().lower()
    if eligibility_type == "social":
        raise RuntimeError("社交登录账号需要先在 ChatGPT Security 中设置密码，再使用失败重试")
    if eligibility_type not in {"", "password", "social_password"}:
        raise _HarApiUnavailable(f"未知换绑资格类型：{eligibility_type}")
    remove_social_subs = eligibility_type == "social_password"

    previous_otp = None
    try:
        previous_otp = mail_api.read_current_otp(api_url, new_email, timeout=4)
    except Exception:
        pass

    progress("submit_new_email", "提交替换邮箱并请求新邮箱验证码")
    if stop_check():
        raise TaskStopRequested("用户已请求停止")
    begin_body = {"email": new_email}
    if remove_social_subs:
        begin_body["remove_social_subs"] = True
    begin = _browser_json_request(
        driver, "POST", "/backend-api/accounts/change_email/begin",
        account_id=account_id, access_token=access_token, body=begin_body,
    )
    if not begin.get("ok"):
        raise RuntimeError("提交替换邮箱后未取得服务端响应，可使用失败重试")
    begin_status = int(begin.get("status") or 0)
    begin_code = _api_error_code(begin.get("data"))
    if begin_status in {404, 405}:
        raise _HarApiUnavailable("换绑开始接口不可用")
    if begin_status == 401 or begin_code == "reauth_required":
        raise _HarApiUnavailable("换绑接口要求页面重新认证")
    if begin_status == 429 or begin_code in {"email_change_rate_limited", "email_change_limit_reached"}:
        raise RuntimeError("当前账号已达到邮箱换绑频率或次数限制")
    if begin_status in {400, 409, 422}:
        detail = f"，错误码 {begin_code}" if begin_code else ""
        raise RuntimeError(f"换绑开始请求被拒绝：HTTP {begin_status}{detail}")
    if begin_status == 403:
        raise ReplacementEmailFailure("email_in_use", "替换邮箱已被其他账号关联")
    if begin_status != 200 or not (
        isinstance(begin.get("data"), dict) and begin["data"].get("success") is True
    ):
        raise RuntimeError(f"提交替换邮箱失败：HTTP {begin_status}")

    progress("wait_new_email_otp", "通过替换邮箱 API 等待新验证码")
    try:
        code = mail_api.wait_for_new_otp(
            api_url, new_email, previous=previous_otp,
            max_wait=settings.OTP_MAX_WAIT, interval=settings.OTP_POLL_INTERVAL,
            stop_check=stop_check,
        )
    except TaskStopRequested:
        raise
    except Exception as exc:
        raise ReplacementEmailFailure(
            "otp_unavailable",
            f"替换邮箱未取得新验证码：{type(exc).__name__}: {str(exc)[:240]}",
        ) from exc

    if stop_check():
        raise TaskStopRequested("用户已请求停止")
    progress("submit_new_email_otp", "提交替换邮箱验证码")
    verify_body = {"email": new_email, "code": code}
    if remove_social_subs:
        verify_body["remove_social_subs"] = True
    verified = _browser_json_request(
        driver, "POST", "/backend-api/accounts/change_email/verify",
        account_id=account_id, access_token=access_token,
        body=verify_body,
    )
    if not verified.get("ok"):
        raise RebindOutcomeUnknown(new_email, "换绑验证码请求已发送，但未取得服务端响应")
    verify_status = int(verified.get("status") or 0)
    if verify_status == 403:
        raise ReplacementEmailFailure("email_in_use", "替换邮箱已被其他账号关联")
    if verify_status == 429:
        raise RuntimeError("替换邮箱验证码校验已达到频率限制")
    if verify_status in {401, 422}:
        raise RuntimeError(f"替换邮箱验证码被拒绝：HTTP {verify_status}，可使用失败重试")
    if verify_status in {400, 409}:
        raise RuntimeError(f"换绑验证请求被拒绝：HTTP {verify_status}，可使用失败重试")
    if verify_status != 200:
        raise RebindOutcomeUnknown(new_email, f"换绑验证码已提交，但服务端返回 HTTP {verify_status}")
    if not (isinstance(verified.get("data"), dict) and verified["data"].get("success") is True):
        raise RebindOutcomeUnknown(new_email, "换绑验证码接口返回 200，但没有 success=true 确认")

    # HAR 中 verify 的 200/{success:true} 是服务端提交点；/auth/logout 是网页
    # onSuccess 回调产生的后续动作。直接同源 fetch 不会自动执行该回调，因此
    # 这里以 200 为已换绑证据，并显式复刻退出动作，不能把 URL 未跳转误判为未知。
    progress("changed", "服务端已确认邮箱更新；退出旧登录态")
    try:
        driver.execute_script("window.location.assign('/auth/logout')")
    except Exception:
        pass


def _change_email_har_guided(
    driver,
    *,
    session: dict,
    new_email: str,
    api_url: str,
    progress: Callable[[str, str], None],
    stop_check: Callable[[], bool] | None = None,
) -> None:
    """只执行已验证的同源 API 换绑协议；API 不可用时直接抛出，由 worker 重试。"""
    _change_email_via_har_api(
        driver, session=session, new_email=new_email,
        api_url=api_url, progress=progress, stop_check=stop_check,
    )


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
        code_input = _visible_input(driver, [
            'input[name="verification_code"]',
            'input[name*="totp" i]', 'input[id*="totp" i]', 'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]', 'input[name="code"]', 'input[type="tel"]',
            'input[id*="verification" i]', 'input[name*="verification" i]',
            'input[aria-label*="code" i]', 'input[data-testid*="code" i]',
        ])
        totp_page = any(marker in text for marker in (
            "authenticator", "two-factor", "2fa", "verification app", "身份验证器", "动态验证码", "인증 앱",
        ))
        if code_input and totp_page and time.monotonic() - totp_sent_at > 8:
            if not str(totp_secret or "").strip():
                raise RuntimeError(f"{email_label}登录要求 2FA，但导入的原邮箱记录没有 MFA Secret")
            progress("login_totp", "提交原账号 2FA 动态码")
            code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
            _set_otp_value(driver, code_input, code)
            _submit_near(driver, code_input)
            totp_sent_at = time.monotonic()
            time.sleep(2)
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
    max_relogin_retries: int | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> tuple[dict, str, str]:
    """换绑提交后只用新邮箱 API 登录；验证码/会话失败时重新走登录页。"""
    retries = settings.MAX_TRANSIENT_RETRIES if max_relogin_retries is None else max_relogin_retries
    total_attempts = max(1, min(int(retries or 0), 10) + 1)
    last_error: Exception | None = None
    stop_check = stop_check or (lambda: False)
    for login_attempt in range(1, total_attempts + 1):
        if stop_check():
            raise TaskStopRequested("用户已请求停止")
        progress(
            "relogin_new",
            f"第 {login_attempt}/{total_attempts} 次使用替换邮箱重新登录：{email}",
        )
        previous_otp = None
        try:
            previous_otp = mail_api.read_current_otp(api_url, email, timeout=4)
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
        submit_email(driver, email, attempts=2)
        try:
            session = _complete_login(
                driver,
                email,
                password,
                totp_secret,
                fetch_session,
                progress,
                email_api_url=api_url,
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


def perform_email_rebind(
    *,
    old_email: str,
    new_email: str,
    password: str,
    totp_secret: str,
    source_api_url: str = "",
    api_url: str,
    proxy_url: str,
    progress: Callable[[str, str], None] | None = None,
    proxy_verified: Callable[[dict], None] | None = None,
    max_relogin_retries: int | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> dict:
    """执行完整闭环：旧邮箱登录 → Settings/Account 换绑 → 新邮箱登录 → 读取新 AT。"""
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
        progress("check_proxy", "检测换绑代理出口；失败时自动切换下一条")
        try:
            opened = client.open_profile(require_proxy_exit_ip=True)
        except Exception as exc:
            message = f"{type(exc).__name__}: {str(exc)[:400]}"
            lowered = message.lower()
            if any(marker in lowered for marker in ("代理出口", "代理格式", "代理协议", "proxy")):
                raise ProxyFailure(message) from exc
            raise
        exit_geo = dict(getattr(opened, "preflight_exit_geo", {}) or {})
        progress("open_roxy", f"代理预检通过（出口 {exit_geo.get('ip') or '已确认'}），创建并打开 Roxy 环境")
        driver = build_driver(opened)
        center_window(driver)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(60)

        # 代理 URL 在本机可用不代表 Roxy 窗口一定正确套用；登录前从实际
        # Selenium 窗口再次读取出口，失败仍属于可安全切换代理的阶段。
        progress("check_proxy", "从 Roxy 窗口复核实际代理出口")
        browser_exit_geo = probe_driver_exit(
            driver, label="Roxy换绑", restore_page_load_timeout=60,
            restore_script_timeout=60, attempts=1, retry_delay=0.5,
        )
        if not browser_exit_geo.get("ip"):
            raise ProxyFailure("Roxy 窗口代理出口复核失败；登录尚未开始")
        exit_geo = dict(browser_exit_geo)
        if callable(proxy_verified):
            proxy_verified(exit_geo)
        progress("open_roxy", f"Roxy 窗口代理检测通过（出口 {exit_geo.get('ip')}）")

        progress("login_old", f"使用原邮箱登录：{old_email}")
        previous_old_login_otp = None
        if source_api_url:
            try:
                previous_old_login_otp = mail_api.read_current_otp(source_api_url, old_email, timeout=4)
            except Exception:
                pass
        safe_get(driver, "https://chatgpt.com/auth/login", timeout=45, attempts=2, accept_hosts=("chatgpt.com", "auth.openai.com"))
        submit_email(driver, old_email, attempts=2)
        old_session = _complete_login(
            driver, old_email, password, totp_secret, fetch_session, progress,
            email_api_url=source_api_url, previous_email_otp=previous_old_login_otp,
            email_label="原邮箱",
            stop_check=stop_check,
        )
        observed_old = _session_email(old_session)
        if observed_old and observed_old.lower() != old_email.lower():
            raise RuntimeError(f"Roxy 登录态邮箱不匹配：期望 {old_email}，实际 {observed_old}")

        _change_email_har_guided(
            driver,
            session=old_session,
            new_email=new_email,
            api_url=api_url,
            progress=progress,
            stop_check=stop_check,
        )

        try:
            new_session, observed_new, access_token = _login_with_replacement_email(
                driver=driver,
                email=new_email,
                password=password,
                totp_secret=totp_secret,
                fetch_session=fetch_session,
                safe_get=safe_get,
                submit_email=submit_email,
                progress=progress,
                api_url=api_url,
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
                f"服务端已显示邮箱更新完成，但新邮箱重新登录/AT 刷新失败：{type(exc).__name__}: {str(exc)[:300]}",
            ) from exc
        progress("verified", "新邮箱登录和 accessToken 校验完成")
        _retain_browser(
            profile_id=opened.profile_id, email=observed_new,
            client=client, opened=opened, driver=driver,
        )
        keep_success_open = True
        progress("kept_open", "AT 已获取；Roxy 窗口保持新邮箱登录态，等待重新获取 AT 或手动关闭")
        return {
            "email": observed_new, "access_token": access_token, "session": new_session,
            "roxy_profile_id": opened.profile_id, "proxy_exit_geo": exit_geo,
            "roxy_browser_status": "open",
        }
    finally:
        # 成功窗口及其 Selenium 连接都保留；任何失败分支立即关闭并删除本轮临时环境。
        if opened is not None and not keep_success_open:
            logger.info(
                "本轮换绑未完成，先关闭并删除 Roxy 临时环境，再由 worker 新建窗口重试：profile=%s",
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
