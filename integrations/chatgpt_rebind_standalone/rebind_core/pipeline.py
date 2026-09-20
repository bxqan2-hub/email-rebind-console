from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .change_email import ChangeEmailClient, ChangeEmailError, password_reauth
from .mail_inbox import wait_code
from .mfa_login import LoginSession, MfaLoginError, login_with_password_and_totp
from .paths import ROOT
from .session_export import build_login_bundle, write_login_bundle


class RebindError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class RebindResult:
    ok: bool
    code: str = "OK"
    message: str = ""
    old_email: str = ""
    new_email: str = ""
    bundle_path: str = ""
    access_token_masked: str = ""
    session_email: str = ""
    trace: list[dict[str, Any]] = field(default_factory=list)
    run_dir: str = ""


def _mask(value: str, head: int = 12, tail: int = 6) -> str:
    text = str(value or "")
    if len(text) <= head + tail:
        return "*" * len(text)
    return f"{text[:head]}...{text[-tail:]}"


def _log(trace: list[dict[str, Any]], step: str, **payload: Any) -> None:
    item = {"time": datetime.now().isoformat(timespec="seconds"), "step": step, **payload}
    # scrub
    for k in list(item.keys()):
        lk = k.lower()
        if any(x in lk for x in ("password", "totp", "secret", "cookie", "token")) and k not in {
            "access_token_masked",
            "has_at",
            "has_session",
        }:
            if isinstance(item[k], str) and len(item[k]) > 12:
                item[k] = _mask(item[k])
    trace.append(item)
    print(f"[{step}] " + ", ".join(f"{k}={v}" for k, v in item.items() if k not in {"time", "step"}))


def run_rebind_email(
    *,
    old_email: str,
    password: str,
    totp_secret: str,
    new_email: str,
    mail_api: str,
    proxy: str | None = None,
    out_dir: str | Path | None = None,
    mail_timeout: float = 120.0,
    mail_poll_interval: float = 2.5,
    progress: Callable[[str, str], None] | None = None,
) -> RebindResult:
    old_email = (old_email or "").strip()
    new_email = (new_email or "").strip()
    password = (password or "").strip()
    totp_secret = (totp_secret or "").strip()
    mail_api = (mail_api or "").strip()
    trace: list[dict[str, Any]] = []
    progress = progress or (lambda _stage, _message: None)
    run_dir = ROOT / "outputs" / "rebind_runs" / (datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8])
    run_dir.mkdir(parents=True, exist_ok=True)

    def _fail(code: str, message: str) -> RebindResult:
        _log(trace, "failed", code=code, message=message)
        (run_dir / "trace.json").write_text(
            json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return RebindResult(
            ok=False,
            code=code,
            message=message,
            old_email=old_email,
            new_email=new_email,
            trace=trace,
            run_dir=str(run_dir),
        )

    try:
        print("[1/6] 旧邮箱账密+TOTP 登录 ...")
        progress("protocol_login_old", "登录原邮箱：建立认证会话")
        login1 = login_with_password_and_totp(
            old_email, password, totp_secret, proxy=proxy,
            progress=lambda message: progress("protocol_login_old", f"登录原邮箱：{message}"),
        )
        _log(
            trace,
            "login_old",
            email=old_email,
            account_id=login1.account_id,
            at=_mask(login1.access_token),
            factor_id=login1.factor_id,
        )

        print("[2/6] 检查 change_email eligibility ...")
        progress("check_email_eligibility", "检查账号是否满足邮箱换绑条件")
        client = ChangeEmailClient(login=login1)
        try:
            elig = client.eligibility()
        except ChangeEmailError as exc:
            return _fail(exc.code, exc.message)
        _log(trace, "eligibility", **{k: elig.get(k) for k in ("eligible", "eligibility_type")})

        print("[3/6] begin 发送新邮箱验证码 ...")
        progress("submit_new_email", "向新邮箱发送验证码")
        issued_after = time.time()
        try:
            begin_resp = client.begin(new_email)
        except ChangeEmailError as exc:
            if exc.code == "REAUTH_FAILED":
                _log(trace, "begin_need_reauth", message=exc.message)
                print("begin 要求 reauth，执行 password+MFA 再试 ...")
                try:
                    progress("protocol_reauth", "服务端要求重新认证，正在验证密码和 2FA")
                    password_reauth(login1)
                    # refresh client tokens
                    client = ChangeEmailClient(login=login1, session_id=str(uuid.uuid4()))
                    progress("submit_new_email", "重新认证完成，向新邮箱发送验证码")
                    begin_resp = client.begin(new_email)
                except Exception as exc2:
                    return _fail("REAUTH_FAILED", str(exc2))
            else:
                return _fail(exc.code, exc.message)
        _log(trace, "begin", new_email=new_email, resp_keys=list(begin_resp.keys())[:10] if isinstance(begin_resp, dict) else [])

        print("[4/6] 等待新邮箱验证码 ...")
        try:
            code = wait_code(
                mail_api, issued_after=issued_after - 5, timeout=mail_timeout,
                poll_interval=mail_poll_interval,
                progress=lambda message: progress("wait_new_email_otp", message),
            )
        except TimeoutError as exc:
            return _fail("MAIL_TIMEOUT", str(exc))
        _log(trace, "mail_code", code_tail=code[-2:])

        print("[5/6] verify 换绑 ...")
        progress("submit_new_email_otp", "已收到验证码，正在提交换绑并等待服务端确认")
        _log(trace, "verify_pending")
        try:
            verify_resp = client.verify(new_email, code)
        except ChangeEmailError as exc:
            return _fail(exc.code, exc.message)
        _log(trace, "verify", resp_keys=list(verify_resp.keys())[:10] if isinstance(verify_resp, dict) else [])
        progress("changed", "服务端已确认换绑，接下来登录新邮箱获取 AT")

        print("[6/6] 新邮箱账密+TOTP 重登并导出 ...")
        progress("protocol_relogin_new", "换绑已完成，正在登录新邮箱获取 AT")
        # 主动重建登录会话
        try:
            login2 = login_with_password_and_totp(
                new_email, password, totp_secret, proxy=proxy,
                progress=lambda message: progress("protocol_relogin_new", f"新邮箱重登：{message}"),
            )
        except MfaLoginError as exc:
            return _fail(exc.code if exc.code in {"LOGIN_FAILED", "MFA_FAILED"} else "RELOGIN_FAILED", exc.message)

        progress("protocol_export", "新邮箱登录完成，正在校验邮箱并保存 AT")
        bundle = build_login_bundle(login2, rebind_email=new_email)
        session_email = str(bundle.get("email") or "")
        if session_email and session_email.lower() != new_email.lower():
            return _fail(
                "RELOGIN_FAILED",
                f"重登后 email 不匹配: got={session_email} expected={new_email}",
            )
        paths = write_login_bundle(bundle, out_dir=out_dir)
        _log(
            trace,
            "export",
            bundle=str(paths["bundle"]),
            email=session_email,
            at=_mask(login2.access_token),
        )
        (run_dir / "trace.json").write_text(
            json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (run_dir / "login_bundle.json").write_text(
            json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return RebindResult(
            ok=True,
            code="OK",
            message="rebind success",
            old_email=old_email,
            new_email=new_email,
            bundle_path=str(paths["bundle"]),
            access_token_masked=_mask(login2.access_token),
            session_email=session_email,
            trace=trace,
            run_dir=str(run_dir),
        )
    except MfaLoginError as exc:
        return _fail(exc.code, exc.message)
    except ChangeEmailError as exc:
        return _fail(exc.code, exc.message)
    except Exception as exc:
        return _fail("EXPORT_FAILED", str(exc))
