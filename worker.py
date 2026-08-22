# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import roxy_flow
import store

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_EXECUTORS: list[ThreadPoolExecutor] = []


def _run(task_id: int) -> None:
    context = store.get_task_context(task_id)
    if not context:
        store.finish_failure(task_id, "任务关联的账号或替换邮箱不存在")
        return
    account = context["account"]
    replacement = context["replacement"]
    store.update_task(task_id, status="running", stage="open_roxy", message="准备打开 Roxy 浏览器")

    def progress(stage: str, message: str) -> None:
        store.update_task(task_id, status="running", stage=stage, message=message)

    try:
        result = roxy_flow.perform_email_rebind(
            old_email=str(account.get("old_email") or ""),
            new_email=str(replacement.get("email") or ""),
            password=str(account.get("password") or ""),
            totp_secret=str(account.get("totp_secret") or ""),
            api_url=str(replacement.get("api_url") or ""),
            progress=progress,
        )
        store.finish_success(task_id, result)
    except Exception as exc:  # noqa: BLE001 - 任务终态需持久化完整异常类型
        logger.exception("换绑任务 #%s 失败", task_id)
        store.finish_failure(task_id, f"{type(exc).__name__}: {str(exc)[:500]}")


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

