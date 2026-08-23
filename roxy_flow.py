# -*- coding: utf-8 -*-
"""通过 Roxy 指纹浏览器执行 ChatGPT 设置里的邮箱换绑。"""
from __future__ import annotations

import logging
import sys
import time
from typing import Callable

import pyotp

import mail_api
import settings

logger = logging.getLogger(__name__)


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


def _complete_login(driver, email: str, password: str, totp_secret: str, fetch_session, progress: Callable[[str, str], None]) -> dict:
    deadline = time.monotonic() + 120
    password_sent = False
    totp_sent_at = 0.0
    while time.monotonic() < deadline:
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
            progress("login_password", "填写账号密码")
            _set_value(driver, password_input, password)
            _submit_near(driver, password_input)
            password_sent = True
            time.sleep(2)
            continue
        text = _body_text(driver).lower()
        code_input = _visible_input(driver, [
            'input[name*="totp" i]', 'input[id*="totp" i]', 'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]', 'input[name="code"]',
        ])
        totp_page = any(marker in text for marker in (
            "authenticator", "two-factor", "2fa", "verification app", "身份验证器", "动态验证码", "인증 앱",
        ))
        if code_input and totp_page and time.monotonic() - totp_sent_at > 8:
            progress("login_totp", "提交原账号 2FA 动态码")
            code = pyotp.TOTP(totp_secret.replace(" ", "")).now()
            _set_value(driver, code_input, code)
            _submit_near(driver, code_input)
            totp_sent_at = time.monotonic()
            time.sleep(2)
            continue
        errors = [line.strip() for line in _body_text(driver).splitlines() if "error" in line.lower() or "错误" in line]
        if errors and password_sent:
            logger.debug("login page messages for %s: %s", email, errors[:3])
        time.sleep(1)
    raise TimeoutError("账号登录超时；未取得 ChatGPT accessToken")


def _open_account_settings(driver, old_email: str, safe_get, progress: Callable[[str, str], None]) -> None:
    progress("open_settings", "打开 ChatGPT 设置 → Account")
    safe_get(driver, "https://chatgpt.com/#settings/Account", timeout=35, attempts=2, accept_hosts=("chatgpt.com",))
    time.sleep(3)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        clicked = driver.execute_script(r"""
        const oldEmail = String(arguments[0] || '').toLowerCase();
        const visible = el => !!el && !el.disabled && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)
          && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
        const all = [...document.querySelectorAll('button, a, [role="button"], [tabindex="0"]')].filter(visible);
        const norm = el => `${el.innerText || ''} ${el.getAttribute('aria-label') || ''} ${el.title || ''}`.trim().toLowerCase();
        let target = all.find(el => oldEmail && norm(el).includes(oldEmail));
        if (!target) target = all.find(el => /change email|update email|edit email|更改邮箱|换绑邮箱|修改邮箱|邮箱地址|email address/.test(norm(el)));
        if (target) { target.scrollIntoView({block:'center'}); target.click(); return 'email'; }
        const account = all.find(el => /^(account|账户|账号|アカウント|계정)$/.test(norm(el)));
        if (account) { account.click(); return 'account'; }
        const settings = all.find(el => /^(settings|设置|設定|설정)$/.test(norm(el)));
        if (settings) { settings.click(); return 'settings'; }
        return '';
        """, old_email)
        if clicked == "email":
            time.sleep(2)
            return
        time.sleep(1)
    raise RuntimeError("设置 → Account 中未找到可点击的当前邮箱；该账号可能不满足自助换绑条件")


def _change_email(
    driver,
    *,
    old_email: str,
    new_email: str,
    password: str,
    totp_secret: str,
    api_url: str,
    progress: Callable[[str, str], None],
) -> None:
    previous_otp = None
    try:
        previous_otp = mail_api.read_current_otp(api_url, new_email, timeout=4)
    except Exception:
        pass
    deadline = time.monotonic() + 210
    new_email_sent = False
    otp_sent = False
    password_sent_at = 0.0
    totp_sent_at = 0.0
    while time.monotonic() < deadline:
        text = _body_text(driver)
        lowered = text.lower()
        current = str(getattr(driver, "current_url", "") or "")
        if otp_sent and (
            "auth/login" in current
            or any(marker in lowered for marker in ("email updated", "email changed", "邮箱已更新", "邮箱地址已更改"))
        ):
            progress("changed", "服务端已完成邮箱更新并退出旧登录态")
            return

        password_input = _visible_input(driver, ['input[type="password"]', 'input[name="password"]'])
        if password_input and time.monotonic() - password_sent_at > 8:
            progress("reauth_password", "换绑前重新验证账号密码")
            _set_value(driver, password_input, password)
            _submit_near(driver, password_input)
            password_sent_at = time.monotonic()
            time.sleep(2)
            continue

        numeric_input = _visible_input(driver, [
            'input[name*="totp" i]', 'input[id*="totp" i]', 'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]', 'input[name="code"]',
        ])
        totp_page = any(marker in lowered for marker in (
            "authenticator", "two-factor", "2fa", "verification app", "身份验证器", "动态验证码", "인증 앱",
        ))
        if numeric_input and totp_page and not otp_sent and time.monotonic() - totp_sent_at > 8:
            progress("reauth_totp", "换绑前重新验证 2FA")
            _set_value(driver, numeric_input, pyotp.TOTP(totp_secret.replace(" ", "")).now())
            _submit_near(driver, numeric_input)
            totp_sent_at = time.monotonic()
            time.sleep(2)
            continue

        email_input = _visible_input(driver, [
            'input[type="email"]', 'input[name*="newEmail" i]', 'input[id*="newEmail" i]',
            'input[autocomplete="email"]',
        ])
        change_context = any(marker in lowered for marker in (
            "new email", "change email", "update email", "新的邮箱", "新邮箱", "更改邮箱", "修改邮箱",
        ))
        if email_input and not new_email_sent and change_context:
            progress("submit_new_email", "填写并提交替换邮箱")
            _set_value(driver, email_input, new_email)
            _submit_near(driver, email_input)
            new_email_sent = True
            time.sleep(3)
            continue

        email_otp_page = numeric_input and new_email_sent and not totp_page and any(marker in lowered for marker in (
            "verification", "one-time", "sent", "code", "验证码", "認証コード", "인증 코드",
        ))
        if email_otp_page and not otp_sent:
            progress("wait_new_email_otp", "通过替换邮箱 API 等待新验证码")
            try:
                code = mail_api.wait_for_new_otp(
                    api_url, new_email, previous=previous_otp,
                    max_wait=settings.OTP_MAX_WAIT, interval=settings.OTP_POLL_INTERVAL,
                    stop_check=lambda: _visible_input(driver, ['input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]', 'input[name="code"]']) is None,
                )
            except Exception as exc:
                raise ReplacementEmailFailure(
                    "otp_unavailable",
                    f"替换邮箱 {new_email} 未取得新验证码：{type(exc).__name__}: {str(exc)[:300]}",
                ) from exc
            progress("submit_new_email_otp", "提交替换邮箱验证码")
            _set_value(driver, numeric_input, code)
            _submit_near(driver, numeric_input)
            otp_sent = True
            time.sleep(3)
            continue

        if new_email_sent and any(marker in lowered for marker in ("already exists", "already in use", "已被使用", "已存在", "邮箱已占用")):
            raise ReplacementEmailFailure("email_in_use", f"替换邮箱 {new_email} 已被其他账号占用")
        if new_email_sent and any(marker in lowered for marker in ("not eligible", "无法更改", "cannot change")):
            raise RuntimeError("当前账号不符合自助换绑条件")
        time.sleep(1)
    if otp_sent:
        raise RebindOutcomeUnknown(new_email, "新邮箱验证码已提交，但没有确认最终换绑结果")
    raise TimeoutError("邮箱换绑流程超时；没有观察到换绑完成后的退出登录状态")


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


def perform_email_rebind(
    *,
    old_email: str,
    new_email: str,
    password: str,
    totp_secret: str,
    api_url: str,
    proxy_url: str,
    progress: Callable[[str, str], None] | None = None,
    proxy_verified: Callable[[dict], None] | None = None,
) -> dict:
    """执行完整闭环：旧邮箱登录 → Settings/Account 换绑 → 新邮箱登录 → 读取新 AT。"""
    progress = progress or (lambda _stage, _message: None)
    RoxyBrowserClient, build_driver, center_window, fetch_session, safe_get, submit_email, probe_driver_exit = _load_main_roxy()
    clean_proxy = str(proxy_url or "").strip()
    if not clean_proxy:
        raise ProxyFailure("任务没有分配换绑代理，已阻止 Roxy 直连")
    client = RoxyBrowserClient(profile_proxy=clean_proxy)
    opened = None
    driver = None
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
        safe_get(driver, "https://chatgpt.com/auth/login", timeout=45, attempts=2, accept_hosts=("chatgpt.com", "auth.openai.com"))
        submit_email(driver, old_email, attempts=2)
        old_session = _complete_login(driver, old_email, password, totp_secret, fetch_session, progress)
        observed_old = _session_email(old_session)
        if observed_old and observed_old.lower() != old_email.lower():
            raise RuntimeError(f"Roxy 登录态邮箱不匹配：期望 {old_email}，实际 {observed_old}")

        _open_account_settings(driver, old_email, safe_get, progress)
        _change_email(
            driver,
            old_email=old_email,
            new_email=new_email,
            password=password,
            totp_secret=totp_secret,
            api_url=api_url,
            progress=progress,
        )

        try:
            progress("relogin_new", f"清理旧登录态并使用替换邮箱重新登录：{new_email}")
            _clear_login_state(driver)
            safe_get(driver, "https://chatgpt.com/auth/login", timeout=45, attempts=2, accept_hosts=("chatgpt.com", "auth.openai.com"))
            submit_email(driver, new_email, attempts=2)
            new_session = _complete_login(driver, new_email, password, totp_secret, fetch_session, progress)
            observed_new = _session_email(new_session)
            if observed_new.lower() != new_email.lower():
                raise RuntimeError(f"换绑后登录邮箱校验失败：期望 {new_email}，实际 {observed_new or '空'}")
            access_token = str(new_session.get("accessToken") or "").strip()
            if not access_token:
                raise RuntimeError("换绑后重新登录成功，但 /api/auth/session 未返回 accessToken")
        except RebindOutcomeUnknown:
            raise
        except Exception as exc:
            raise RebindOutcomeUnknown(
                new_email,
                f"服务端已显示邮箱更新完成，但新邮箱重新登录/AT 刷新失败：{type(exc).__name__}: {str(exc)[:300]}",
            ) from exc
        progress("verified", "新邮箱登录和 accessToken 校验完成")
        return {
            "email": observed_new, "access_token": access_token, "session": new_session,
            "roxy_profile_id": opened.profile_id, "proxy_exit_geo": exit_geo,
        }
    finally:
        if driver is not None and not settings.KEEP_BROWSER_OPEN:
            try:
                driver.quit()
            except Exception:
                pass
        if opened is not None and not settings.KEEP_BROWSER_OPEN:
            client.cleanup_profile(opened)
