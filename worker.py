# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import roxy_flow
import settings
import store

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_EXECUTORS: list[ThreadPoolExecutor] = []


def _run(task_id: int) -> None:
    current_task_id = int(task_id)
    active_proxy: dict | None = None
    excluded_proxy_ids: set[int] = set()
    proxy_attempt = 0
    while True:
        context = store.get_task_context(current_task_id)
        if not context:
            store.finish_failure(current_task_id, "任务关联的账号或替换邮箱不存在")
            return
        task = context["task"]
        account = context["account"]
        replacement = context["replacement"]
        attempt = int(task.get("attempt") or 1)
        if active_proxy is None:
            if proxy_attempt >= settings.MAX_PROXY_ATTEMPTS:
                store.finish_failure(
                    current_task_id,
                    f"换绑代理已达到自动切换上限 {settings.MAX_PROXY_ATTEMPTS} 条",
                )
                return
            active_proxy = store.pick_random_proxy(excluded_proxy_ids)
            if not active_proxy:
                store.finish_failure(
                    current_task_id,
                    "换绑代理池没有可用代理；失败代理已保留原因，请补充或重新启用后重试",
                )
                return
            proxy_attempt += 1
            excluded_proxy_ids.add(int(active_proxy.get("id") or 0))
        store.assign_task_proxy(current_task_id, active_proxy, proxy_attempt)
        proxy_display = str(active_proxy.get("display") or f"代理 #{active_proxy.get('id')}")
        store.update_task(
            current_task_id, status="running", stage="open_roxy",
            message=f"第 {attempt} 次邮箱尝试 / 第 {proxy_attempt} 条代理：使用 {proxy_display} 检测并打开 Roxy",
        )

        def progress(stage: str, message: str) -> None:
            store.update_task(
                current_task_id, status="running", stage=stage,
                message=f"第 {attempt} 次邮箱尝试 / 第 {proxy_attempt} 条代理：{message}",
            )

        def proxy_verified(exit_geo: dict) -> None:
            store.mark_proxy_success(
                int(active_proxy.get("id") or 0), task_id=current_task_id,
                old_email=str(account.get("old_email") or ""), exit_geo=exit_geo,
            )

        try:
            result = roxy_flow.perform_email_rebind(
                old_email=str(account.get("old_email") or ""),
                new_email=str(replacement.get("email") or ""),
                password=str(account.get("password") or ""),
                totp_secret=str(account.get("totp_secret") or ""),
                api_url=str(replacement.get("api_url") or ""),
                proxy_url=str(active_proxy.get("proxy_url") or ""),
                progress=progress,
                proxy_verified=proxy_verified,
            )
            store.finish_success(current_task_id, result)
            return
        except roxy_flow.ProxyFailure as exc:
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
        except roxy_flow.ReplacementEmailFailure as exc:
            message = f"{type(exc).__name__}: {str(exc)[:500]}"
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
            logger.error("换绑结果待人工核验：task=%s new_email=%s reason=%s", current_task_id, exc.new_email, message)
            store.finish_review_failure(current_task_id, exc.new_email, message)
            return
        except Exception as exc:  # noqa: BLE001 - 任务终态需持久化完整异常类型
            logger.exception("换绑任务 #%s 失败", current_task_id)
            store.finish_failure(current_task_id, f"{type(exc).__name__}: {str(exc)[:500]}")
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
