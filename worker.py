# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import roxy_flow
import roxy_browser_rebind as browser_rebind
import protocol_flow
import settings
import store
import trial_check
from totp_source import resolve_totp_secret

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_EXECUTORS: list[ThreadPoolExecutor] = []
_ACTION_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="email-rebind-action")
_TRIAL_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="email-rebind-trial")
_TRIAL_PROXY_RETRIES = 5


_TRANSIENT_STAGES = {
    "queued", "running", "protocol_login_old", "protocol_upstream", "protocol_reauth", "check_proxy", "login_old", "login_password", "login_totp",
    "login_email_otp", "submit_login_email_otp", "check_email_eligibility",
    "submit_new_email",
}
_NON_RETRYABLE_MARKERS = (
    "不符合自助换绑条件", "社交登录账号", "需要密码", "没有 mfa", "没有对应的 api",
    "频率或次数限制", "频率限制", "次数限制", "被拒绝", "rate limit",
    "not_eligible", "eligible=false", "用户名或密码错误", "totp 校验失败",
    "email/password/totp_secret 不能为空",
)


def _should_auto_retry(task: dict, exc: Exception) -> bool:
    """仅重试服务端/浏览器临时故障，绝不跨过验证码已提交的未知结果。"""
    if isinstance(exc, (roxy_flow.ProxyFailure, roxy_flow.ReplacementEmailFailure, roxy_flow.RebindOutcomeUnknown)):
        return False
    message = str(exc or "").strip().lower()
    if any(marker.lower() in message for marker in _NON_RETRYABLE_MARKERS):
        return False
    stage = str(task.get("stage") or "").strip()
    if stage == "submit_new_email_otp":
        return any(marker in message for marker in ("未取得服务端响应", "可使用失败重试", "http 401", "http 422", "http 400", "http 409"))
    if stage in {"changed", "relogin_new", "protocol_relogin_new", "verified", "protocol_verified", "kept_open", "manual_review", "wait_new_email_otp"}:
        return False
    return isinstance(exc, TimeoutError) or stage in _TRANSIENT_STAGES


def _refresh_access_token(account_id: int) -> None:
    account = store.get_success_account_context(account_id)
    if not account:
        store.fail_access_token_refresh(account_id, "成功账号不存在")
        return
    profile_id = str(account.get("roxy_profile_id") or "").strip()
    expected_email = str(account.get("new_email") or account.get("current_email") or "").strip()
    roxy_open = account.get("roxy_browser_status") == "open" and bool(profile_id)
    try:
        raw_totp = str(account.get("totp_secret") or "").strip()
        totp_secret = resolve_totp_secret(raw_totp) if raw_totp else ""
        if roxy_open:
            logger.info("成功账号 #%s 检测到 Roxy 窗口，复用窗口更新 AT", account_id)
            result = roxy_flow.refresh_retained_access_token(profile_id, expected_email)
        else:
            proxy = store.pick_random_proxy() or {}
            proxy_url = str(proxy.get("proxy_url") or "").strip()
            logger.info(
                "成功账号 #%s 未检测到 Roxy 窗口，执行一次纯协议登录更新 AT%s",
                account_id, f"（代理 {proxy.get('display') or proxy.get('id')}）" if proxy_url else "",
            )
            result = protocol_flow.refresh_access_token_protocol(
                email=expected_email,
                password=str(account.get("password") or ""),
                totp_secret=totp_secret,
                proxy_url=proxy_url,
            )
        previous_token = str(account.get("access_token") or "").strip()
        refreshed_token = str(result.get("access_token") or "").strip()
        if refreshed_token == previous_token and refreshed_token.count(".") == 2:
            # A same-token response is only accepted when the token still
            # answers the authoritative /backend-api/me validity check.
            check = trial_check.check_access_token_validity(refreshed_token)
            if check.get("valid") is not True:
                raise RuntimeError(
                    f"Roxy session 返回的 AT 未通过有效性确认："
                    f"{check.get('error') or check.get('outcome') or 'check_error'}"
                )
        store.finish_access_token_refresh(account_id, result)
        submit_trial_check(account_id)
    except Exception as exc:  # noqa: BLE001 - 操作结果必须回写页面状态
        logger.exception("成功账号 #%s 重新获取 AT 失败", account_id)
        store.fail_access_token_refresh(
            account_id, f"{type(exc).__name__}: {str(exc)[:500]}",
            browser_open=roxy_open and roxy_flow.retained_profile_is_connected(profile_id),
        )


def submit_access_token_refresh(account_id: int) -> dict:
    account = store.begin_access_token_refresh(account_id)
    if not account:
        current = store.get_success_account_context(account_id)
        if current and current.get("at_refresh_status") == "running":
            return {"accepted": False, "busy": True, "error": "该账号正在重新获取 AT"}
        return {"accepted": False, "busy": False, "error": "成功账号不存在或无法启动 AT 检查"}
    try:
        _ACTION_EXECUTOR.submit(_refresh_access_token, int(account_id))
    except Exception as exc:
        store.fail_access_token_refresh(account_id, f"后台任务提交失败：{type(exc).__name__}: {exc}")
        raise
    return {"accepted": True, "account_id": int(account_id)}


def submit_trial_check(account_id: int) -> dict:
    account = store.get_success_account_context(account_id)
    if not account or not str(account.get("access_token") or "").strip():
        return {"accepted": False, "busy": False, "error": "成功账号暂无 AT"}
    if not store.list_detection_proxies():
        store.finish_trial_check(account_id, {"ok": False, "error": "未配置资格检测代理"})
        return {"accepted": False, "busy": False, "error": "未配置资格检测代理"}
    current = store.begin_trial_check(account_id)
    if not current:
        account = store.get_success_account_context(account_id) or {}
        if account.get("trial_check_status") == "running":
            return {"accepted": False, "busy": True, "error": "该账号正在检测资格"}
        return {"accepted": False, "busy": False, "error": "账号无法启动资格检测"}
    # begin_trial_check marks it running; hand the captured context directly to
    # avoid claiming the same account a second time inside the worker.
    def run_captured() -> None:
        claim = current
        proxy = claim["proxy"]
        last_result = None
        attempted_proxy_ids = {int(proxy.get("id") or 0)}
        for attempt in range(_TRIAL_PROXY_RETRIES):
            try:
                result = trial_check.check_zero_trial(
                    claim["access_token"],
                    f'{proxy.get("country") or ""}|{proxy.get("proxy_url") or ""}',
                )
            except Exception as exc:
                result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}", "trial_proxy_country": str(proxy.get("country") or "").upper()}
            last_result = result
            store.finish_trial_check(account_id, result)
            store.mark_detection_proxy_result(int(proxy.get("id") or 0), result)
            if result.get("trial_zero_trial_eligible") is True:
                break
            terminal = bool(
                result.get("plan_terminal_code")
                or result.get("account_unusable_code")
                or result.get("credential_unusable_code")
                or result.get("token_expired") is True
            )
            if terminal:
                break
            # A successful API response without a promotion can be exit-IP
            # specific. Transient proxy failures also move to the next proxy.
            next_claim = store.begin_trial_check(account_id, exclude_proxy_ids=attempted_proxy_ids)
            if not next_claim:
                break
            claim = next_claim
            proxy = claim["proxy"]
            attempted_proxy_ids.add(int(proxy.get("id") or 0))
        # Every retry claims the account again, so an exhausted loop would
        # otherwise leave the final claim stuck in ``running`` forever.
        if last_result is not None:
            latest = store.get_success_account_context(account_id) or {}
            if latest.get("trial_check_status") in {"queued", "running"}:
                store.finish_trial_check(account_id, last_result)
    try:
        _TRIAL_EXECUTOR.submit(run_captured)
    except Exception as exc:
        store.finish_trial_check(account_id, {"ok": False, "error": f"后台资格检测提交失败：{type(exc).__name__}: {exc}"})
        raise
    return {"accepted": True, "account_id": int(account_id), "status": "running"}


def _open_roxy_after_protocol(
    *, task_id: int, task: dict, account: dict, replacement: dict,
    protocol_result: dict, protocol_proxy: dict,
    retry_limit: int, stop_check,
) -> dict:
    """换绑已经成功后，额外选择另一条代理创建 Roxy 登录窗口。"""
    excluded = {int(protocol_proxy.get("id") or 0)}
    last_error = "没有可用于 Roxy 扩展登录的第二条代理"
    for roxy_attempt in range(1, settings.MAX_PROXY_ATTEMPTS + 1):
        if stop_check():
            break
        roxy_proxy = store.pick_random_proxy(excluded)
        if not roxy_proxy:
            break
        proxy_id = int(roxy_proxy.get("id") or 0)
        excluded.add(proxy_id)
        proxy_display = str(roxy_proxy.get("display") or f"代理 #{proxy_id}")
        store.update_task(
            task_id, status="running", stage="open_roxy_after_protocol",
            message=f"纯协议换绑已完成；第 {roxy_attempt} 条额外代理 {proxy_display} 正在打开 Roxy",
            roxy_proxy_id=proxy_id, roxy_proxy_display=proxy_display,
        )

        def roxy_progress(stage: str, message: str) -> None:
            if stop_check():
                raise roxy_flow.TaskStopRequested("用户已请求停止")
            store.update_task(
                task_id, status="running", stage=f"roxy_{stage}",
                message=f"纯协议换绑已完成；Roxy 扩展：{message}",
                email_change_confirmed=True,
            )

        def roxy_proxy_verified(exit_geo: dict) -> None:
            store.mark_proxy_success(
                proxy_id, task_id=task_id,
                old_email=str(account.get("old_email") or ""), exit_geo=exit_geo,
            )

        try:
            roxy_result = roxy_flow.perform_replacement_login(
                new_email=str(replacement.get("email") or ""),
                password=str(account.get("password") or ""),
                totp_secret=str(account.get("totp_secret") or ""),
                api_url=str(replacement.get("api_url") or ""),
                auth_method=str(account.get("auth_method") or ""),
                proxy_url=str(roxy_proxy.get("proxy_url") or ""),
                progress=roxy_progress,
                proxy_verified=roxy_proxy_verified,
                max_relogin_retries=retry_limit,
                stop_check=stop_check,
            )
            return {
                **protocol_result, **roxy_result,
                "protocol_engine": protocol_result.get("protocol_engine"),
                "protocol_upstream_commit": protocol_result.get("protocol_upstream_commit"),
                "roxy_open_requested": True,
                "roxy_proxy_id": proxy_id,
                "roxy_proxy_display": proxy_display,
            }
        except roxy_flow.ProxyFailure as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            store.mark_proxy_failure(
                proxy_id, task_id=task_id,
                old_email=str(account.get("old_email") or ""), error=last_error,
            )
            continue
        except roxy_flow.TaskStopRequested:
            last_error = "用户在纯协议完成后停止了 Roxy 扩展登录"
            break
        except Exception as exc:  # 换绑已经成功，Roxy 扩展失败不能回滚身份
            last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            logger.exception("纯协议已成功，但 Roxy 扩展登录失败：task=%s", task_id)
            break
    return {
        **protocol_result,
        "roxy_open_requested": True,
        "roxy_browser_status": "open_failed",
        "roxy_open_error": last_error,
    }


def _run(task_id: int) -> None:
    current_task_id = int(task_id)
    active_proxy: dict | None = None
    excluded_proxy_ids: set[int] = set()
    proxy_attempt = 0
    allow_proxy_reuse = False
    while True:
        context = store.get_task_context(current_task_id)
        if not context:
            store.finish_failure(current_task_id, "任务关联的账号或替换邮箱不存在")
            return
        task = context["task"]
        account = context["account"]
        replacement = context["replacement"]
        if store.is_task_stop_requested(current_task_id):
            store.finish_stopped(current_task_id)
            return
        attempt = int(task.get("attempt") or 1)
        login_only = bool(task.get("login_only"))
        change_confirmed = bool(login_only or task.get("email_change_confirmed"))
        retry_limit = max(0, min(
            int(task.get("max_transient_retries", settings.MAX_TRANSIENT_RETRIES) or 0), 10,
        ))
        if active_proxy is None:
            if proxy_attempt >= settings.MAX_PROXY_ATTEMPTS:
                message = f"换绑代理已达到自动切换上限 {settings.MAX_PROXY_ATTEMPTS} 条"
                if login_only:
                    store.finish_review_failure(current_task_id, str(replacement.get("email") or ""), message)
                else:
                    store.finish_failure(current_task_id, message)
                return
            active_proxy = store.pick_random_proxy(excluded_proxy_ids)
            if not active_proxy and allow_proxy_reuse:
                # 临时故障重试优先换未用代理；只有代理池本身没有第二条时，
                # 才允许复用当前代理，避免单代理配置直接失去自动重试机会。
                excluded_proxy_ids.clear()
                active_proxy = store.pick_random_proxy()
            allow_proxy_reuse = False
            if not active_proxy:
                message = "换绑代理池没有可用代理；失败代理已保留原因，请补充或重新启用后重试"
                if login_only:
                    store.finish_review_failure(current_task_id, str(replacement.get("email") or ""), message)
                else:
                    store.finish_failure(current_task_id, message)
                return
            proxy_attempt += 1
            excluded_proxy_ids.add(int(active_proxy.get("id") or 0))
        store.assign_task_proxy(current_task_id, active_proxy, proxy_attempt)
        proxy_display = str(active_proxy.get("display") or f"代理 #{active_proxy.get('id')}")
        initial_stage = "open_roxy" if login_only else "protocol_login_old"
        initial_message = (
            f"第 {attempt} 次登录补救 / 第 {proxy_attempt} 条代理：使用 {proxy_display} 打开 Roxy"
            if login_only else
            f"第 {attempt} 次纯协议换绑 / 第 {proxy_attempt} 条代理：使用 {proxy_display} 登录原邮箱"
        )
        store.update_task(
            current_task_id, status="running", stage=initial_stage,
            message=initial_message,
        )

        def progress(stage: str, message: str) -> None:
            nonlocal change_confirmed
            if stage == "changed":
                change_confirmed = True
            if store.is_task_stop_requested(current_task_id):
                raise roxy_flow.TaskStopRequested("用户已请求停止")
            # 同步本轮真实阶段，避免前一条代理失败留下的 proxy_failed 阶段
            # 阻断 submit_new_email 临时故障的既有自动重试机制。
            task["stage"] = stage
            store.update_task(
                current_task_id, status="running", stage=stage,
                message=f"第 {attempt} 次邮箱尝试 / 第 {proxy_attempt} 条代理：{message}",
                email_change_confirmed=change_confirmed,
            )

        def stop_check() -> bool:
            return store.is_task_stop_requested(current_task_id)

        def proxy_verified(exit_geo: dict) -> None:
            store.mark_proxy_success(
                int(active_proxy.get("id") or 0), task_id=current_task_id,
                old_email=str(account.get("old_email") or ""), exit_geo=exit_geo,
            )

        try:
            raw_totp = str(account.get("totp_secret") or "").strip()
            totp_secret = resolve_totp_secret(raw_totp) if raw_totp else ""
            if login_only:
                result = roxy_flow.perform_replacement_login(
                    new_email=str(replacement.get("email") or account.get("current_email") or ""),
                    password=str(account.get("password") or ""),
                    totp_secret=totp_secret,
                    api_url=str(replacement.get("api_url") or ""),
                    auth_method=str(account.get("auth_method") or ""),
                    proxy_url=str(active_proxy.get("proxy_url") or ""),
                    progress=progress,
                    proxy_verified=proxy_verified,
                    max_relogin_retries=retry_limit,
                    stop_check=stop_check,
                )
            elif str(task.get("rebind_mode") or "protocol") == "browser":
                result = browser_rebind.perform_email_rebind(
                    old_email=str(account.get("old_email") or ""),
                    new_email=str(replacement.get("email") or ""),
                    password=str(account.get("password") or ""),
                    totp_secret=totp_secret,
                    source_api_url=str(account.get("api_url") or ""),
                    auth_method=str(account.get("auth_method") or ""),
                    api_url=str(replacement.get("api_url") or ""),
                    proxy_url=str(active_proxy.get("proxy_url") or ""),
                    progress=progress,
                    proxy_verified=proxy_verified,
                    max_relogin_retries=retry_limit,
                    stop_check=stop_check,
                )
            else:
                result = protocol_flow.run_upstream_rebind(
                    old_email=str(account.get("old_email") or ""),
                    new_email=str(replacement.get("email") or ""),
                    password=str(account.get("password") or ""),
                    totp_secret=totp_secret,
                    api_url=str(replacement.get("api_url") or ""),
                    proxy_url=str(active_proxy.get("proxy_url") or ""),
                    progress=progress,
                    stop_check=stop_check,
                )
                change_confirmed = True
                store.mark_proxy_success(
                    int(active_proxy.get("id") or 0), task_id=current_task_id,
                    old_email=str(account.get("old_email") or ""),
                )
                if bool(task.get("open_roxy_after")):
                    result = _open_roxy_after_protocol(
                        task_id=current_task_id, task=task, account=account,
                        replacement=replacement, protocol_result=result,
                        protocol_proxy=active_proxy, retry_limit=retry_limit,
                        stop_check=stop_check,
                    )
            store.finish_success(current_task_id, result)
            submit_trial_check(int(context["account"]["id"]))
            return
        except roxy_flow.TaskStopRequested as exc:
            logger.info("换绑任务 #%s 收到停止请求：%s", current_task_id, exc)
            store.finish_stopped(current_task_id)
            return
        except roxy_flow.ProxyFailure as exc:
            if store.is_task_stop_requested(current_task_id):
                store.finish_stopped(current_task_id)
                return
            message = f"{type(exc).__name__}: {str(exc)[:500]}"
            logger.warning(
                "换绑代理检测失败，准备自动切换：task=%s proxy_id=%s reason=%s",
                current_task_id, active_proxy.get("id"), message,
            )
            store.mark_proxy_failure(
                int(active_proxy.get("id") or 0), task_id=current_task_id,
                old_email=str(account.get("old_email") or ""), error=message,
            )
            store.update_task(
                current_task_id, status="running", stage="proxy_failed",
                message=f"{proxy_display} 检测失败并已标记不可用；正在随机切换下一条代理",
            )
            active_proxy = None
            continue
        except protocol_flow.ProtocolSessionFailure as exc:
            if store.is_task_stop_requested(current_task_id):
                store.finish_stopped(current_task_id)
                return
            message = f"{type(exc).__name__}: {str(exc)[:500]}"
            logger.warning(
                "纯协议 OAuth 会话失效，重建会话并切换代理：task=%s proxy_id=%s reason=%s",
                current_task_id, active_proxy.get("id"), message,
            )
            store.update_task(
                current_task_id, status="running", stage="protocol_session_failed",
                message=f"{proxy_display} 的 OAuth 会话已失效；正在用下一条代理重建完整登录会话",
            )
            active_proxy = None
            continue
        except roxy_flow.ReplacementEmailFailure as exc:
            message = f"{type(exc).__name__}: {str(exc)[:500]}"
            if store.is_task_stop_requested(current_task_id):
                store.finish_stopped(current_task_id)
                return
            if login_only:
                logger.error("已换绑账号补救登录失败：task=%s email=%s reason=%s", current_task_id, replacement.get("email"), message)
                store.finish_review_failure(current_task_id, str(replacement.get("email") or ""), message)
                return
            logger.warning("替换邮箱失败，准备自动轮换：task=%s email=%s reason=%s", current_task_id, replacement.get("email"), message)
            rotation = store.rotate_failed_replacement(
                current_task_id, message, exc.code, settings.MAX_REPLACEMENT_ATTEMPTS,
            )
            next_task = rotation.get("next_task")
            if not next_task:
                return
            current_task_id = int(next_task["id"])
        except roxy_flow.RebindOutcomeUnknown as exc:
            message = f"{type(exc).__name__}: {str(exc)[:500]}"
            if store.is_task_stop_requested(current_task_id):
                store.finish_stopped(current_task_id)
                return
            logger.error("换绑结果待人工核验：task=%s new_email=%s reason=%s", current_task_id, exc.new_email, message)
            store.finish_review_failure(current_task_id, exc.new_email, message)
            return
        except Exception as exc:  # noqa: BLE001 - 任务终态需持久化完整异常类型
            message = f"{type(exc).__name__}: {str(exc)[:500]}"
            if store.is_task_stop_requested(current_task_id):
                store.finish_stopped(current_task_id)
                return
            if login_only:
                logger.error("已换绑账号补救登录失败：task=%s email=%s reason=%s", current_task_id, replacement.get("email"), message)
                store.finish_review_failure(current_task_id, str(replacement.get("email") or ""), message)
                return
            if _should_auto_retry(task, exc):
                rotation = store.retry_transient_failure(
                    current_task_id, message, retry_limit,
                )
                next_task = rotation.get("next_task")
                if next_task:
                    logger.warning(
                        "换绑任务 #%s 遇到临时故障，自动重试第 %s 次：%s",
                        current_task_id, next_task.get("attempt"), message,
                    )
                    active_proxy = None
                    allow_proxy_reuse = True
                    current_task_id = int(next_task["id"])
                    if settings.TRANSIENT_RETRY_DELAY:
                        deadline = time.monotonic() + settings.TRANSIENT_RETRY_DELAY
                        while time.monotonic() < deadline:
                            if store.is_task_stop_requested(current_task_id):
                                store.finish_stopped(current_task_id)
                                return
                            time.sleep(min(0.25, deadline - time.monotonic()))
                    continue
                if rotation.get("reason") == "attempt_limit":
                    logger.error(
                        "换绑任务 #%s 临时故障重试耗尽：%s", current_task_id, message,
                    )
                    return
            logger.exception("换绑任务 #%s 失败", current_task_id)
            store.finish_failure(current_task_id, message)
            return


def submit_tasks(tasks: list[dict], workers: int) -> int:
    if not tasks:
        return 0
    executor = ThreadPoolExecutor(max_workers=max(1, min(int(workers or 1), len(tasks), 10)), thread_name_prefix="email-rebind")
    with _LOCK:
        _EXECUTORS.append(executor)
    futures = [executor.submit(_run, int(task["id"])) for task in tasks]

    def shutdown_when_done() -> None:
        for future in futures:
            try:
                future.result()
            except Exception:
                pass
        executor.shutdown(wait=False)
        with _LOCK:
            if executor in _EXECUTORS:
                _EXECUTORS.remove(executor)

    threading.Thread(target=shutdown_when_done, daemon=True, name="email-rebind-batch-cleanup").start()
    return len(tasks)
