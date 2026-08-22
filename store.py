# -*- coding: utf-8 -*-
"""换绑分站的独立 JSON 持久化层，不读取或改写主站运行数据。"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import settings

_LOCK = threading.RLock()
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_ACCOUNTS = settings.DATA_DIR / "source_accounts.json"
_REPLACEMENTS = settings.DATA_DIR / "替换邮箱.json"
_TASKS = settings.DATA_DIR / "rebind_tasks.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _next_id(rows: list[dict]) -> int:
    return max((int(row.get("id") or 0) for row in rows), default=0) + 1


def _parts(line: str) -> list[str]:
    delimiter = "----" if "----" in line else "====" if "====" in line else ""
    return [part.strip() for part in line.split(delimiter)] if delimiter else []


def _public_account(row: dict) -> dict:
    return {
        key: value for key, value in row.items()
        if key not in {"password", "totp_secret", "access_token"}
    } | {
        "has_password": bool(row.get("password")),
        "has_totp": bool(row.get("totp_secret")),
        "has_access_token": bool(row.get("access_token")),
    }


def _public_replacement(row: dict) -> dict:
    return {
        key: value for key, value in row.items()
        if key != "api_url"
    } | {"has_api": bool(row.get("api_url"))}


def import_source_accounts(text: str) -> dict:
    """识别主站导出格式：邮箱----密码----MFA Secret。"""
    parsed: list[dict] = []
    invalid: list[dict] = []
    for number, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _parts(line)
        if len(parts) < 3 or not _EMAIL_RE.match(parts[0]) or not parts[1] or not parts[2]:
            invalid.append({"line": number, "reason": "需要：邮箱----密码----MFA Secret"})
            continue
        parsed.append({"email": parts[0], "password": parts[1], "totp_secret": parts[2]})

    with _LOCK:
        rows = _read(_ACCOUNTS)
        by_email = {str(row.get("old_email") or "").lower(): row for row in rows}
        inserted = updated = 0
        now = _now()
        for item in parsed:
            key = item["email"].lower()
            row = by_email.get(key)
            if row is None:
                row = {
                    "id": _next_id(rows),
                    "old_email": item["email"],
                    "current_email": item["email"],
                    "status": "ready",
                    "created_at": now,
                }
                rows.append(row)
                by_email[key] = row
                inserted += 1
            else:
                updated += 1
                if row.get("status") not in {"running", "success"}:
                    row["status"] = "ready"
                    row["error"] = ""
            row["password"] = item["password"]
            row["totp_secret"] = item["totp_secret"]
            row["updated_at"] = now
        _write(_ACCOUNTS, rows)
    return {"parsed": len(parsed), "inserted": inserted, "updated": updated, "invalid": invalid}


def import_replacement_emails(text: str) -> dict:
    """导入“替换邮箱”号池：新邮箱----API取码地址。"""
    parsed: list[dict] = []
    invalid: list[dict] = []
    for number, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _parts(line)
        if len(parts) < 2 or not _EMAIL_RE.match(parts[0]) or not parts[1].lower().startswith(("http://", "https://")):
            invalid.append({"line": number, "reason": "需要：新邮箱----http(s)://API取码地址"})
            continue
        parsed.append({"email": parts[0], "api_url": parts[1]})

    with _LOCK:
        rows = _read(_REPLACEMENTS)
        by_email = {str(row.get("email") or "").lower(): row for row in rows}
        inserted = updated = 0
        now = _now()
        for item in parsed:
            key = item["email"].lower()
            row = by_email.get(key)
            if row is None:
                row = {
                    "id": _next_id(rows),
                    "email": item["email"],
                    "status": "available",
                    "pool": "替换邮箱",
                    "created_at": now,
                }
                rows.append(row)
                by_email[key] = row
                inserted += 1
            else:
                updated += 1
                if row.get("status") not in {"reserved", "used"}:
                    row["status"] = "available"
                    row["error"] = ""
            row["api_url"] = item["api_url"]
            row["updated_at"] = now
        _write(_REPLACEMENTS, rows)
    return {"parsed": len(parsed), "inserted": inserted, "updated": updated, "invalid": invalid}


def list_accounts() -> list[dict]:
    with _LOCK:
        return [_public_account(row) for row in sorted(_read(_ACCOUNTS), key=lambda x: int(x.get("id") or 0))]


def list_replacements() -> list[dict]:
    with _LOCK:
        return [_public_replacement(row) for row in sorted(_read(_REPLACEMENTS), key=lambda x: int(x.get("id") or 0))]


def list_tasks(limit: int = 500) -> list[dict]:
    with _LOCK:
        rows = sorted(_read(_TASKS), key=lambda x: int(x.get("id") or 0), reverse=True)
        return [dict(row) for row in rows[:max(1, int(limit or 500))]]


def summary() -> dict:
    with _LOCK:
        accounts = _read(_ACCOUNTS)
        replacements = _read(_REPLACEMENTS)
        tasks = _read(_TASKS)
        return {
            "accounts_total": len(accounts),
            "accounts_ready": sum(1 for row in accounts if row.get("status") in {"ready", "failed"}),
            "accounts_success": sum(1 for row in accounts if row.get("status") == "success"),
            "replacement_total": len(replacements),
            "replacement_available": sum(1 for row in replacements if row.get("status") == "available"),
            "tasks_active": sum(1 for row in tasks if row.get("status") in {"queued", "running"}),
            "tasks_failed": sum(1 for row in tasks if row.get("status") == "failed"),
        }


def preview_pairs(account_ids: Iterable[int] | None = None) -> list[dict]:
    wanted = {int(value) for value in (account_ids or [])}
    with _LOCK:
        accounts = [
            row for row in sorted(_read(_ACCOUNTS), key=lambda x: int(x.get("id") or 0))
            if row.get("status") in {"ready", "failed"} and (not wanted or int(row.get("id") or 0) in wanted)
        ]
        replacements = [
            row for row in sorted(_read(_REPLACEMENTS), key=lambda x: int(x.get("id") or 0))
            if row.get("status") == "available"
        ]
        return [
            {
                "account_id": int(account.get("id") or 0),
                "old_email": account.get("old_email"),
                "replacement_id": int(replacement.get("id") or 0),
                "new_email": replacement.get("email"),
            }
            for account, replacement in zip(accounts, replacements)
        ]


def reserve_batch(account_ids: Iterable[int] | None = None) -> list[dict]:
    pairs = preview_pairs(account_ids)
    if not pairs:
        return []
    with _LOCK:
        accounts = _read(_ACCOUNTS)
        replacements = _read(_REPLACEMENTS)
        tasks = _read(_TASKS)
        account_by_id = {int(row.get("id") or 0): row for row in accounts}
        replacement_by_id = {int(row.get("id") or 0): row for row in replacements}
        created: list[dict] = []
        now = _now()
        for pair in pairs:
            account = account_by_id.get(pair["account_id"])
            replacement = replacement_by_id.get(pair["replacement_id"])
            if not account or not replacement:
                continue
            if account.get("status") not in {"ready", "failed"} or replacement.get("status") != "available":
                continue
            task = {
                "id": _next_id(tasks),
                "account_id": pair["account_id"],
                "replacement_id": pair["replacement_id"],
                "old_email": pair["old_email"],
                "new_email": pair["new_email"],
                "status": "queued",
                "stage": "queued",
                "message": "已完成一对一占用，等待 Roxy 浏览器",
                "created_at": now,
            }
            tasks.append(task)
            created.append(dict(task))
            account.update({"status": "queued", "active_task_id": task["id"], "updated_at": now})
            replacement.update({
                "status": "reserved", "active_task_id": task["id"],
                "bound_old_email": pair["old_email"], "updated_at": now,
            })
        _write(_ACCOUNTS, accounts)
        _write(_REPLACEMENTS, replacements)
        _write(_TASKS, tasks)
        return created


def get_task_context(task_id: int) -> dict | None:
    with _LOCK:
        task = next((row for row in _read(_TASKS) if int(row.get("id") or 0) == int(task_id)), None)
        if not task:
            return None
        account = next((row for row in _read(_ACCOUNTS) if int(row.get("id") or 0) == int(task.get("account_id") or 0)), None)
        replacement = next((row for row in _read(_REPLACEMENTS) if int(row.get("id") or 0) == int(task.get("replacement_id") or 0)), None)
        if not account or not replacement:
            return None
        return {"task": dict(task), "account": dict(account), "replacement": dict(replacement)}


def update_task(task_id: int, *, status: str | None = None, stage: str | None = None, message: str | None = None) -> bool:
    with _LOCK:
        tasks = _read(_TASKS)
        task = next((row for row in tasks if int(row.get("id") or 0) == int(task_id)), None)
        if not task:
            return False
        now = _now()
        if status:
            task["status"] = status
            if status == "running" and not task.get("started_at"):
                task["started_at"] = now
        if stage:
            task["stage"] = stage
        if message is not None:
            task["message"] = str(message)[:600]
        task["updated_at"] = now
        _write(_TASKS, tasks)
        return True


def finish_success(task_id: int, result: dict) -> None:
    with _LOCK:
        tasks = _read(_TASKS)
        accounts = _read(_ACCOUNTS)
        replacements = _read(_REPLACEMENTS)
        task = next(row for row in tasks if int(row.get("id") or 0) == int(task_id))
        account = next(row for row in accounts if int(row.get("id") or 0) == int(task.get("account_id") or 0))
        replacement = next(row for row in replacements if int(row.get("id") or 0) == int(task.get("replacement_id") or 0))
        now = _now()
        access_token = str(result.get("access_token") or "").strip()
        verified_email = str(result.get("email") or task.get("new_email") or "").strip()
        task.update({
            "status": "success", "stage": "completed", "message": "换绑完成；新邮箱重新登录成功，AT 已刷新",
            "completed_at": now, "updated_at": now, "verified_email": verified_email,
        })
        account.update({
            "status": "success", "current_email": verified_email, "new_email": verified_email,
            "access_token": access_token, "rebound_at": now, "updated_at": now,
        })
        replacement.update({
            "status": "used", "used_at": now, "updated_at": now,
            "bound_old_email": task.get("old_email"), "bound_account_id": account.get("id"),
        })
        _write(_TASKS, tasks)
        _write(_ACCOUNTS, accounts)
        _write(_REPLACEMENTS, replacements)


def finish_failure(task_id: int, error: str) -> None:
    with _LOCK:
        tasks = _read(_TASKS)
        accounts = _read(_ACCOUNTS)
        replacements = _read(_REPLACEMENTS)
        task = next((row for row in tasks if int(row.get("id") or 0) == int(task_id)), None)
        if not task:
            return
        account = next((row for row in accounts if int(row.get("id") or 0) == int(task.get("account_id") or 0)), None)
        replacement = next((row for row in replacements if int(row.get("id") or 0) == int(task.get("replacement_id") or 0)), None)
        now = _now()
        message = str(error or "换绑失败")[:600]
        task.update({"status": "failed", "stage": "failed", "message": message, "error": message, "completed_at": now, "updated_at": now})
        if account:
            account.update({"status": "failed", "error": message, "updated_at": now})
            account.pop("active_task_id", None)
        if replacement:
            replacement.update({"status": "available", "error": message, "updated_at": now})
            for key in ("active_task_id", "bound_old_email"):
                replacement.pop(key, None)
        _write(_TASKS, tasks)
        _write(_ACCOUNTS, accounts)
        _write(_REPLACEMENTS, replacements)


def recover_interrupted_tasks() -> int:
    active_ids = [int(row.get("id") or 0) for row in _read(_TASKS) if row.get("status") in {"queued", "running"}]
    for task_id in active_ids:
        finish_failure(task_id, "分站进程重启，原任务已释放，可重新开始")
    return len(active_ids)


def export_success_lines() -> list[str]:
    with _LOCK:
        rows = [row for row in _read(_ACCOUNTS) if row.get("status") == "success"]
        rows.sort(key=lambda row: int(row.get("id") or 0))
        return [
            "----".join([
                str(row.get("new_email") or row.get("current_email") or "").strip(),
                str(row.get("password") or "").strip(),
                str(row.get("totp_secret") or "").strip(),
                str(row.get("access_token") or "").strip(),
            ])
            for row in rows
            if all(str(row.get(key) or "").strip() for key in ("password", "totp_secret", "access_token"))
            and str(row.get("new_email") or row.get("current_email") or "").strip()
        ]

