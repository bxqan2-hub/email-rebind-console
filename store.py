# -*- coding: utf-8 -*-
"""换绑分站的独立 JSON 持久化层，不读取或改写主站运行数据。"""
from __future__ import annotations

import json
import os
import random
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import settings

_LOCK = threading.RLock()
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_ACCOUNTS = settings.DATA_DIR / "source_accounts.json"
_REPLACEMENTS = settings.DATA_DIR / "替换邮箱.json"
_TASKS = settings.DATA_DIR / "rebind_tasks.json"
_PROXIES = settings.DATA_DIR / "换绑代理.json"
_PROXY_RANDOM = random.SystemRandom()
_SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


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
        if key not in {"password", "totp_secret", "api_url", "replacement_api_url", "access_token"}
    } | {
        "has_password": bool(row.get("password")),
        "has_totp": bool(row.get("totp_secret")),
        "has_api": bool(row.get("api_url")),
        "has_replacement_api": bool(row.get("replacement_api_url")),
        "has_access_token": bool(row.get("access_token")),
        "at_saved": bool(row.get("access_token")),
    }


def _public_replacement(row: dict) -> dict:
    return {
        key: value for key, value in row.items()
        if key != "api_url"
    } | {"has_api": bool(row.get("api_url"))}


def _clean_proxy_text(raw: str) -> str:
    return str(raw or "").strip().strip("\"'").translate(str.maketrans({
        "：": ":", "／": "/", "＠": "@", "．": ".",
    }))


def _normalize_proxy_url(raw: str, username: str = "", password: str = "") -> str:
    """统一代理格式并返回 Roxy 可直接使用的 URL。"""
    value = _clean_proxy_text(raw)
    if not value:
        raise ValueError("代理为空")
    if "://" not in value:
        if "@" in value:
            value = f"http://{value}"
        else:
            chunks = value.split(":")
            if len(chunks) >= 4 and chunks[1].isdigit():
                host, port = chunks[0], chunks[1]
                username = username or chunks[2]
                password = password or ":".join(chunks[3:])
                # 供应商的 host:port:user:pass 通常是 SOCKS5 凭据；此前误按
                # HTTP CONNECT 导致 1024proxy 节点全部在创建 Roxy 前失败。
                value = f"socks5h://{host}:{port}"
            else:
                value = f"{'socks5h' if username else 'http'}://{value}"
    parsed = urlsplit(value)
    scheme = str(parsed.scheme or "").lower()
    if scheme not in _SUPPORTED_PROXY_SCHEMES:
        raise ValueError(f"不支持的代理协议：{scheme or '-'}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理端口不是有效整数") from exc
    if not parsed.hostname or not port or not (1 <= int(port) <= 65535):
        raise ValueError("代理需要有效的 host:port")
    user = str(username or (unquote(parsed.username) if parsed.username else "")).strip()
    secret = str(password or (unquote(parsed.password) if parsed.password else "")).strip()
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = ""
    if user:
        auth = quote(user, safe="")
        if secret:
            auth += f":{quote(secret, safe='')}"
        auth += "@"
    return urlunsplit((scheme, f"{auth}{host}:{port}", "", "", ""))


def _proxy_display(proxy_url: str) -> str:
    parsed = urlsplit(str(proxy_url or ""))
    host = parsed.hostname or "-"
    port = f":{parsed.port}" if parsed.port else ""
    auth = "***:***@" if parsed.username or parsed.password else ""
    return f"{parsed.scheme}://{auth}{host}{port}"


def _public_proxy(row: dict) -> dict:
    public = {key: value for key, value in row.items() if key != "proxy_url"}
    public["display"] = _proxy_display(str(row.get("proxy_url") or ""))
    public["has_auth"] = bool(urlsplit(str(row.get("proxy_url") or "")).username)
    return public


def import_proxies(text: str) -> dict:
    """导入换绑代理池；带认证但未写协议的格式默认按 SOCKS5 处理。"""
    parsed: list[dict] = []
    invalid: list[dict] = []
    for number, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _parts(line)
        try:
            if len(parts) == 3:
                proxy_url = _normalize_proxy_url(parts[0], parts[1], parts[2])
            elif len(parts) > 1:
                raise ValueError("认证代理需要：host:port----用户名----密码")
            else:
                proxy_url = _normalize_proxy_url(line)
        except ValueError as exc:
            invalid.append({"line": number, "reason": str(exc)})
            continue
        parsed.append({"proxy_url": proxy_url})

    with _LOCK:
        rows = _read(_PROXIES)
        by_url = {str(row.get("proxy_url") or "").lower(): row for row in rows}
        inserted = updated = 0
        now = _now()
        for item in parsed:
            key = item["proxy_url"].lower()
            row = by_url.get(key)
            if row is None:
                row = {
                    "id": _next_id(rows), "proxy_url": item["proxy_url"],
                    "status": "available", "created_at": now,
                }
                rows.append(row)
                by_url[key] = row
                inserted += 1
            else:
                updated += 1
                # 与失败邮箱一致：重复导入不悄悄恢复坏代理，需人工重新启用。
                if row.get("status") != "failed":
                    row["status"] = "available"
            row["updated_at"] = now
        _write(_PROXIES, rows)
    return {"parsed": len(parsed), "inserted": inserted, "updated": updated, "invalid": invalid}


def list_proxies() -> list[dict]:
    with _LOCK:
        return [_public_proxy(row) for row in sorted(_read(_PROXIES), key=lambda item: int(item.get("id") or 0))]


def pick_random_proxy(excluded_ids: Iterable[int] | None = None) -> dict | None:
    excluded = {int(value) for value in (excluded_ids or [])}
    with _LOCK:
        rows = _read(_PROXIES)
        candidates = [
            row for row in rows
            if row.get("status") == "available" and int(row.get("id") or 0) not in excluded
        ]
        if not candidates:
            return None
        row = _PROXY_RANDOM.choice(candidates)
        now = _now()
        row["assigned_count"] = int(row.get("assigned_count") or 0) + 1
        row["last_assigned_at"] = now
        row["updated_at"] = now
        _write(_PROXIES, rows)
        return dict(row) | {"display": _proxy_display(str(row.get("proxy_url") or ""))}


def mark_proxy_success(proxy_id: int, *, task_id: int, old_email: str, exit_geo: dict | None = None) -> bool:
    with _LOCK:
        rows = _read(_PROXIES)
        row = next((item for item in rows if int(item.get("id") or 0) == int(proxy_id)), None)
        if not row:
            return False
        now = _now()
        geo = exit_geo or {}
        row.update({
            "status": "available", "success_count": int(row.get("success_count") or 0) + 1,
            "last_success_at": now, "last_task_id": int(task_id),
            "last_old_email": str(old_email or ""), "updated_at": now,
        })
        if geo.get("ip"):
            row["last_exit_ip"] = str(geo.get("ip"))
        if geo.get("country"):
            row["last_country"] = str(geo.get("country"))
        row.pop("last_error", None)
        _write(_PROXIES, rows)
        return True


def mark_proxy_failure(proxy_id: int, *, task_id: int, old_email: str, error: str) -> bool:
    with _LOCK:
        rows = _read(_PROXIES)
        row = next((item for item in rows if int(item.get("id") or 0) == int(proxy_id)), None)
        if not row:
            return False
        now = _now()
        message = str(error or "代理检测失败")[:600]
        row.update({
            "status": "failed", "failure_reason": message, "last_error": message,
            "failure_count": int(row.get("failure_count") or 0) + 1,
            "failed_at": now, "last_task_id": int(task_id),
            "last_old_email": str(old_email or ""), "updated_at": now,
        })
        _write(_PROXIES, rows)
        return True


def restore_proxy(proxy_id: int) -> dict | None:
    with _LOCK:
        rows = _read(_PROXIES)
        row = next((item for item in rows if int(item.get("id") or 0) == int(proxy_id)), None)
        if not row or row.get("status") != "failed":
            return None
        now = _now()
        row.update({"status": "available", "restored_at": now, "updated_at": now})
        for key in ("failure_reason", "failed_at", "last_error"):
            row.pop(key, None)
        _write(_PROXIES, rows)
        return _public_proxy(row)


def delete_proxy(proxy_id: int) -> dict:
    """删除未被活动任务使用的代理；任务历史保留掩码与原代理 ID。"""
    with _LOCK:
        rows = _read(_PROXIES)
        row = next((item for item in rows if int(item.get("id") or 0) == int(proxy_id)), None)
        if not row:
            return {"deleted": False, "reason": "not_found"}
        active_task = next((
            task for task in _read(_TASKS)
            if int(task.get("proxy_id") or 0) == int(proxy_id)
            and task.get("status") in {"queued", "running"}
        ), None)
        if active_task:
            return {
                "deleted": False,
                "reason": "in_use",
                "task_id": int(active_task.get("id") or 0),
            }
        rows.remove(row)
        _write(_PROXIES, rows)
        return {"deleted": True, "item": _public_proxy(row)}


def delete_all_proxies() -> dict:
    """删除全部未被活动任务使用的代理，并保留被占用代理。"""
    with _LOCK:
        rows = _read(_PROXIES)
        active_tasks = {
            int(task.get("proxy_id") or 0): task
            for task in _read(_TASKS)
            if task.get("status") in {"queued", "running"}
            and int(task.get("proxy_id") or 0) > 0
        }
        kept: list[dict] = []
        deleted: list[dict] = []
        skipped: list[dict] = []
        for row in rows:
            proxy_id = int(row.get("id") or 0)
            active_task = active_tasks.get(proxy_id)
            if active_task:
                kept.append(row)
                skipped.append({
                    "id": proxy_id,
                    "task_id": int(active_task.get("id") or 0),
                })
                continue
            deleted.append(_public_proxy(row))
        _write(_PROXIES, kept)
        return {
            "deleted": len(deleted),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "items": deleted,
        }


def assign_task_proxy(task_id: int, proxy: dict, proxy_attempt: int) -> bool:
    with _LOCK:
        tasks = _read(_TASKS)
        task = next((row for row in tasks if int(row.get("id") or 0) == int(task_id)), None)
        if not task:
            return False
        task.update({
            "proxy_id": int(proxy.get("id") or 0),
            "proxy_display": str(proxy.get("display") or _proxy_display(str(proxy.get("proxy_url") or ""))),
            "proxy_attempt": int(proxy_attempt), "updated_at": _now(),
        })
        _write(_TASKS, tasks)
        return True


def import_source_accounts(text: str) -> dict:
    """只导入原邮箱账号，支持密码+2FA或邮箱API取码两种登录资料。"""
    parsed: list[dict] = []
    invalid: list[dict] = []
    for number, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _parts(line)
        email_valid = bool(parts and _EMAIL_RE.match(parts[0]))
        if (
            email_valid
            and len(parts) == 2
            and parts[1].lower().startswith(("http://", "https://"))
        ):
            parsed.append({"email": parts[0], "api_url": parts[1], "auth_method": "email_api"})
            continue
        if email_valid and len(parts) >= 3 and parts[1] and parts[2]:
            parsed.append({
                "email": parts[0], "password": parts[1],
                "totp_secret": parts[2], "auth_method": "password_totp",
            })
            continue
        invalid.append({
            "line": number,
            "reason": "原邮箱需要：邮箱----http(s)://API取码地址，或 邮箱----密码----MFA Secret",
        })

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
            row["auth_method"] = item["auth_method"]
            if item["auth_method"] == "email_api":
                row["api_url"] = item["api_url"]
                row.pop("password", None)
                row.pop("totp_secret", None)
            else:
                row["password"] = item["password"]
                row["totp_secret"] = item["totp_secret"]
                row.pop("api_url", None)
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
                # 已隔离的失败邮箱必须显式重新启用，重复导入不能悄悄回到可用池。
                if row.get("status") not in {"reserved", "used", "failed"}:
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


def delete_source_account(account_id: int) -> dict:
    """删除空闲的原邮箱账号；成功账号需先关闭保留窗口。"""
    with _LOCK:
        rows = _read(_ACCOUNTS)
        row = next((item for item in rows if int(item.get("id") or 0) == int(account_id)), None)
        if not row:
            return {"deleted": False, "reason": "not_found"}
        active_task = next((
            task for task in _read(_TASKS)
            if int(task.get("account_id") or 0) == int(account_id)
            and task.get("status") in {"queued", "running"}
        ), None)
        if row.get("status") in {"queued", "running"} or active_task:
            return {
                "deleted": False,
                "reason": "in_use",
                "task_id": int((active_task or {}).get("id") or row.get("active_task_id") or 0),
            }
        if row.get("status") == "success" and row.get("roxy_browser_status") != "deleted":
            return {"deleted": False, "reason": "window_open"}
        if row.get("status") == "review":
            return {"deleted": False, "reason": "result_locked"}
        rows.remove(row)
        _write(_ACCOUNTS, rows)
        return {"deleted": True, "item": _public_account(row)}


def list_tasks(limit: int = 500) -> list[dict]:
    with _LOCK:
        rows = sorted(_read(_TASKS), key=lambda x: int(x.get("id") or 0), reverse=True)
        return [dict(row) for row in rows[:max(1, int(limit or 500))]]


def _remove_task_links(rows: list[dict], removed_ids: set[int]) -> None:
    """删除已结束日志后，清掉剩余任务中指向已删除日志的追踪字段。"""
    for row in rows:
        for key in ("retry_of_task_id", "root_task_id", "manual_retry_task_id", "next_task_id"):
            if int(row.get(key) or 0) in removed_ids:
                row.pop(key, None)


def delete_failed_task(task_id: int) -> dict:
    """仅清理已失败的任务日志，不改动账号、邮箱池或成功结果。"""
    with _LOCK:
        rows = _read(_TASKS)
        row = next((item for item in rows if int(item.get("id") or 0) == int(task_id)), None)
        if not row:
            return {"deleted": False, "reason": "not_found"}
        if row.get("status") != "failed":
            return {"deleted": False, "reason": "not_failed"}
        rows.remove(row)
        _remove_task_links(rows, {int(task_id)})
        _write(_TASKS, rows)
        return {"deleted": True, "item": dict(row)}


def delete_finished_task(task_id: int) -> dict:
    """清理单条已结束任务日志，保留账号、邮箱池和导出结果。"""
    with _LOCK:
        rows = _read(_TASKS)
        row = next((item for item in rows if int(item.get("id") or 0) == int(task_id)), None)
        if not row:
            return {"deleted": False, "reason": "not_found"}
        if row.get("status") not in {"success", "failed", "review"}:
            return {"deleted": False, "reason": "not_finished"}
        rows.remove(row)
        _remove_task_links(rows, {int(task_id)})
        _write(_TASKS, rows)
        return {"deleted": True, "item": dict(row)}


def clear_failed_tasks() -> dict:
    """批量清理全部失败任务日志，保留进行中和成功记录。"""
    with _LOCK:
        rows = _read(_TASKS)
        removed = [row for row in rows if row.get("status") == "failed"]
        removed_ids = {int(row.get("id") or 0) for row in removed}
        kept = [row for row in rows if row.get("status") != "failed"]
        if removed_ids:
            _remove_task_links(kept, removed_ids)
            _write(_TASKS, kept)
        return {"deleted": len(removed), "task_ids": sorted(removed_ids)}


def clear_finished_tasks() -> dict:
    """批量清理成功、失败和待核验任务日志，活动任务保持锁定。"""
    with _LOCK:
        rows = _read(_TASKS)
        finished = {"success", "failed", "review"}
        removed = [row for row in rows if row.get("status") in finished]
        removed_ids = {int(row.get("id") or 0) for row in removed}
        kept = [row for row in rows if row.get("status") not in finished]
        if removed_ids:
            _remove_task_links(kept, removed_ids)
            _write(_TASKS, kept)
        return {"deleted": len(removed), "task_ids": sorted(removed_ids)}


def summary() -> dict:
    with _LOCK:
        accounts = _read(_ACCOUNTS)
        replacements = _read(_REPLACEMENTS)
        proxies = _read(_PROXIES)
        tasks = _read(_TASKS)
        return {
            "accounts_total": len(accounts),
            "accounts_ready": sum(1 for row in accounts if row.get("status") in {"ready", "failed"}),
            "accounts_success": sum(1 for row in accounts if row.get("status") == "success"),
            "roxy_open": sum(
                1 for row in accounts
                if row.get("status") == "success" and row.get("roxy_browser_status") == "open"
            ),
            "replacement_total": len(replacements),
            "replacement_available": sum(1 for row in replacements if row.get("status") == "available"),
            "replacement_failed": sum(1 for row in replacements if row.get("status") == "failed"),
            "replacement_review": sum(1 for row in replacements if row.get("status") == "review"),
            "proxy_total": len(proxies),
            "proxy_available": sum(1 for row in proxies if row.get("status") == "available"),
            "proxy_failed": sum(1 for row in proxies if row.get("status") == "failed"),
            "accounts_review": sum(1 for row in accounts if row.get("status") == "review"),
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


def reserve_batch(
    account_ids: Iterable[int] | None = None,
    *,
    max_transient_retries: int | None = None,
) -> list[dict]:
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
                "attempt": 1,
                "message": "已完成一对一占用，等待 Roxy 浏览器",
                "created_at": now,
            }
            if max_transient_retries is not None:
                task["max_transient_retries"] = max(0, min(int(max_transient_retries), 10))
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


def reserve_failed_account_retry(
    account_id: int,
    *,
    max_transient_retries: int | None = None,
) -> dict:
    """为一个已失败账号建立可追溯的人工重试任务。"""
    with _LOCK:
        accounts = _read(_ACCOUNTS)
        replacements = _read(_REPLACEMENTS)
        tasks = _read(_TASKS)
        account = next(
            (row for row in accounts if int(row.get("id") or 0) == int(account_id)),
            None,
        )
        if not account:
            return {"task": None, "reason": "not_found"}
        if account.get("status") != "failed":
            return {"task": None, "reason": "not_failed"}
        if any(
            int(row.get("account_id") or 0) == int(account_id)
            and row.get("status") in {"queued", "running"}
            for row in tasks
        ):
            return {"task": None, "reason": "busy"}
        replacement = next((
            row for row in sorted(replacements, key=lambda item: int(item.get("id") or 0))
            if row.get("status") == "available"
        ), None)
        if not replacement:
            return {"task": None, "reason": "no_replacement"}

        previous = next((
            row for row in sorted(tasks, key=lambda item: int(item.get("id") or 0), reverse=True)
            if int(row.get("account_id") or 0) == int(account_id)
            and row.get("status") == "failed"
        ), None)
        now = _now()
        attempt = int((previous or {}).get("attempt") or 1) + 1
        task = {
            "id": _next_id(tasks),
            "account_id": int(account.get("id") or 0),
            "replacement_id": int(replacement.get("id") or 0),
            "old_email": account.get("old_email"),
            "new_email": replacement.get("email"),
            "status": "queued",
            "stage": "manual_retry",
            "attempt": attempt,
            "message": f"第 {attempt} 次尝试：失败重试已提交，等待 Roxy 浏览器",
            "created_at": now,
            "updated_at": now,
        }
        if max_transient_retries is not None:
            task["max_transient_retries"] = max(0, min(int(max_transient_retries), 10))
        if previous:
            task["root_task_id"] = int(previous.get("root_task_id") or previous.get("id") or 0)
            task["retry_of_task_id"] = int(previous.get("id") or 0)
            previous["manual_retry_task_id"] = task["id"]
            previous["updated_at"] = now
        tasks.append(task)
        previous_error = str(account.get("error") or "").strip()
        account.update({"status": "queued", "active_task_id": task["id"], "updated_at": now})
        account.pop("error", None)
        if previous_error:
            account["last_error"] = previous_error
        replacement.update({
            "status": "reserved",
            "active_task_id": task["id"],
            "bound_old_email": account.get("old_email"),
            "updated_at": now,
        })
        _write(_TASKS, tasks)
        _write(_ACCOUNTS, accounts)
        _write(_REPLACEMENTS, replacements)
        return {"task": dict(task), "reason": "reserved"}


def finish_replacement_failure(task_id: int, error: str, failure_code: str) -> bool:
    """隔离收不到验证码/已占用的替换邮箱，并释放原账号等待自动轮换。"""
    with _LOCK:
        tasks = _read(_TASKS)
        accounts = _read(_ACCOUNTS)
        replacements = _read(_REPLACEMENTS)
        task = next((row for row in tasks if int(row.get("id") or 0) == int(task_id)), None)
        if not task:
            return False
        account = next((row for row in accounts if int(row.get("id") or 0) == int(task.get("account_id") or 0)), None)
        replacement = next((row for row in replacements if int(row.get("id") or 0) == int(task.get("replacement_id") or 0)), None)
        now = _now()
        message = str(error or "替换邮箱不可用")[:600]
        code = str(failure_code or "replacement_failed")[:80]
        task.update({
            "status": "failed", "stage": "replacement_failed", "message": message,
            "error": message, "failure_code": code, "retryable": True,
            "completed_at": now, "updated_at": now,
        })
        if account:
            account.update({
                "status": "ready", "error": message, "last_replacement_failure": message,
                "replacement_attempts": int(task.get("attempt") or 1), "updated_at": now,
            })
            account.pop("active_task_id", None)
        if replacement:
            replacement.update({
                "status": "failed", "failure_code": code, "failure_reason": message,
                "failed_at": now, "updated_at": now,
                "failure_count": int(replacement.get("failure_count") or 0) + 1,
                "bound_old_email": task.get("old_email"),
            })
            replacement.pop("active_task_id", None)
        _write(_TASKS, tasks)
        _write(_ACCOUNTS, accounts)
        _write(_REPLACEMENTS, replacements)
        return True


def reserve_retry(previous_task_id: int) -> dict | None:
    """为同一原账号原子占用下一个可用替换邮箱，并建立可追溯的新任务。"""
    with _LOCK:
        tasks = _read(_TASKS)
        accounts = _read(_ACCOUNTS)
        replacements = _read(_REPLACEMENTS)
        previous = next((row for row in tasks if int(row.get("id") or 0) == int(previous_task_id)), None)
        if not previous:
            return None
        if previous.get("status") != "failed" or previous.get("stage") != "replacement_failed":
            return None
        account = next((row for row in accounts if int(row.get("id") or 0) == int(previous.get("account_id") or 0)), None)
        replacement = next((
            row for row in sorted(replacements, key=lambda item: int(item.get("id") or 0))
            if row.get("status") == "available"
        ), None)
        if not account or not replacement or account.get("status") == "success":
            return None
        now = _now()
        attempt = int(previous.get("attempt") or 1) + 1
        task = {
            "id": _next_id(tasks),
            "account_id": int(account.get("id") or 0),
            "replacement_id": int(replacement.get("id") or 0),
            "old_email": account.get("old_email"),
            "new_email": replacement.get("email"),
            "status": "queued", "stage": "auto_retry", "attempt": attempt,
            "root_task_id": int(previous.get("root_task_id") or previous.get("id") or 0),
            "retry_of_task_id": int(previous.get("id") or 0),
            "message": f"第 {attempt} 次尝试：已自动切换替换邮箱，等待 Roxy 浏览器",
            "created_at": now, "updated_at": now,
        }
        tasks.append(task)
        previous["next_task_id"] = task["id"]
        previous["message"] = f"{str(previous.get('message') or '')[:420]}；已自动切换到 {replacement.get('email')}"
        previous["updated_at"] = now
        account.update({"status": "queued", "active_task_id": task["id"], "updated_at": now})
        replacement.update({
            "status": "reserved", "active_task_id": task["id"],
            "bound_old_email": account.get("old_email"), "updated_at": now,
        })
        _write(_TASKS, tasks)
        _write(_ACCOUNTS, accounts)
        _write(_REPLACEMENTS, replacements)
        return dict(task)


def rotate_failed_replacement(task_id: int, error: str, failure_code: str, max_attempts: int) -> dict:
    """在同一临界区隔离坏邮箱并优先为原账号占用下一个，避免并发抢走替换邮箱。"""
    with _LOCK:
        context = get_task_context(task_id)
        if not context:
            return {"next_task": None, "reason": "missing_task"}
        attempt = int(context["task"].get("attempt") or 1)
        finish_replacement_failure(task_id, error, failure_code)
        limit = max(1, int(max_attempts or 1))
        if attempt >= limit:
            message = f"已达到自动轮换上限 {limit} 次；最后失败：{error}"
            mark_retry_exhausted(task_id, message)
            return {"next_task": None, "reason": "attempt_limit", "message": message}
        next_task = reserve_retry(task_id)
        if next_task:
            return {"next_task": next_task, "reason": "rotated"}
        message = f"替换邮箱已标记不可用；号池没有更多可用邮箱。最后失败：{error}"
        mark_retry_exhausted(task_id, message)
        return {"next_task": None, "reason": "pool_exhausted", "message": message}


def finish_review_failure(task_id: int, new_email: str, error: str) -> None:
    """换绑结果不确定时同时冻结账号和替换邮箱，禁止重复换绑造成身份错位。"""
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
        clean_email = str(new_email or task.get("new_email") or "").strip()
        message = str(error or "换绑结果待人工核验")[:600]
        task.update({
            "status": "failed", "stage": "manual_review", "message": message,
            "error": message, "retryable": False, "completed_at": now, "updated_at": now,
        })
        if account:
            account.update({
                "status": "review", "new_email": clean_email, "current_email": clean_email,
                "email_change_uncertain": True, "error": message, "updated_at": now,
            })
            account.pop("active_task_id", None)
        if replacement:
            replacement.update({
                "status": "review", "failure_code": "outcome_unknown", "failure_reason": message,
                "failed_at": now, "updated_at": now, "bound_old_email": task.get("old_email"),
            })
            replacement.pop("active_task_id", None)
        _write(_TASKS, tasks)
        _write(_ACCOUNTS, accounts)
        _write(_REPLACEMENTS, replacements)


def mark_retry_exhausted(task_id: int, message: str) -> None:
    """号池耗尽或达到轮换上限时，把原账号留在可人工重试的失败态。"""
    with _LOCK:
        tasks = _read(_TASKS)
        accounts = _read(_ACCOUNTS)
        task = next((row for row in tasks if int(row.get("id") or 0) == int(task_id)), None)
        if not task:
            return
        account = next((row for row in accounts if int(row.get("id") or 0) == int(task.get("account_id") or 0)), None)
        now = _now()
        clean = str(message or "没有更多可用替换邮箱")[:600]
        task.update({"auto_retry_status": "exhausted", "message": clean, "updated_at": now})
        if account:
            account.update({"status": "failed", "error": clean, "updated_at": now})
            account.pop("active_task_id", None)
        _write(_TASKS, tasks)
        _write(_ACCOUNTS, accounts)


def restore_replacement(replacement_id: int) -> dict | None:
    """人工确认接口/邮箱恢复后，将失败邮箱重新放回可用池。"""
    with _LOCK:
        rows = _read(_REPLACEMENTS)
        row = next((item for item in rows if int(item.get("id") or 0) == int(replacement_id)), None)
        if not row or row.get("status") != "failed":
            return None
        now = _now()
        row.update({"status": "available", "restored_at": now, "updated_at": now})
        for key in ("error", "failure_code", "failure_reason", "failed_at", "active_task_id", "bound_old_email"):
            row.pop(key, None)
        _write(_REPLACEMENTS, rows)
        return _public_replacement(row)


def delete_replacement(replacement_id: int) -> dict:
    """删除未被活动任务占用的替换邮箱；已完成任务和账号结果不受影响。"""
    with _LOCK:
        rows = _read(_REPLACEMENTS)
        row = next((item for item in rows if int(item.get("id") or 0) == int(replacement_id)), None)
        if not row:
            return {"deleted": False, "reason": "not_found"}
        active_task = next((
            task for task in _read(_TASKS)
            if int(task.get("replacement_id") or 0) == int(replacement_id)
            and task.get("status") in {"queued", "running"}
        ), None)
        if row.get("status") == "reserved" or active_task:
            task_id = int(
                (active_task or {}).get("id")
                or row.get("active_task_id")
                or 0
            )
            return {"deleted": False, "reason": "in_use", "task_id": task_id}
        rows.remove(row)
        _write(_REPLACEMENTS, rows)
        return {"deleted": True, "item": _public_replacement(row)}


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


def get_success_account_context(account_id: int) -> dict | None:
    with _LOCK:
        account = next((
            row for row in _read(_ACCOUNTS)
            if int(row.get("id") or 0) == int(account_id)
        ), None)
        if not account or account.get("status") != "success":
            return None
        return dict(account)


def get_success_access_token(account_id: int) -> str | None:
    with _LOCK:
        account = next((
            row for row in _read(_ACCOUNTS)
            if int(row.get("id") or 0) == int(account_id)
            and row.get("status") == "success"
        ), None)
        access_token = str((account or {}).get("access_token") or "").strip()
        return access_token or None


def begin_access_token_refresh(account_id: int) -> dict | None:
    with _LOCK:
        rows = _read(_ACCOUNTS)
        row = next((item for item in rows if int(item.get("id") or 0) == int(account_id)), None)
        if (
            not row
            or row.get("status") != "success"
            or row.get("roxy_browser_status") != "open"
            or not row.get("roxy_profile_id")
        ):
            return None
        if row.get("at_refresh_status") == "running":
            return None
        now = _now()
        row.update({
            "at_refresh_status": "running", "at_refresh_started_at": now,
            "updated_at": now,
        })
        row.pop("at_refresh_error", None)
        row.pop("at_token_changed", None)
        _write(_ACCOUNTS, rows)
        return dict(row)


def finish_access_token_refresh(account_id: int, result: dict) -> dict | None:
    with _LOCK:
        rows = _read(_ACCOUNTS)
        row = next((item for item in rows if int(item.get("id") or 0) == int(account_id)), None)
        if not row or row.get("status") != "success":
            return None
        access_token = str(result.get("access_token") or "").strip()
        if not access_token:
            raise ValueError("重新获取结果缺少 access_token")
        token_changed = access_token != str(row.get("access_token") or "").strip()
        now = _now()
        row.update({
            "access_token": access_token, "at_refresh_status": "success",
            "at_token_changed": token_changed,
            "at_refreshed_at": now, "at_saved_at": now, "roxy_browser_status": "open",
            "updated_at": now,
        })
        row.pop("at_refresh_error", None)
        _write(_ACCOUNTS, rows)
        persisted = next((
            item for item in _read(_ACCOUNTS)
            if int(item.get("id") or 0) == int(account_id)
        ), None)
        if not persisted or str(persisted.get("access_token") or "").strip() != access_token:
            raise OSError("重新获取的 AT 持久化校验失败")
        return _public_account(persisted)


def fail_access_token_refresh(account_id: int, error: str, *, browser_open: bool = False) -> dict | None:
    with _LOCK:
        rows = _read(_ACCOUNTS)
        row = next((item for item in rows if int(item.get("id") or 0) == int(account_id)), None)
        if not row or row.get("status") != "success":
            return None
        now = _now()
        row.update({
            "at_refresh_status": "failed", "at_refresh_error": str(error or "重新获取 AT 失败")[:600],
            "updated_at": now,
        })
        if browser_open:
            row["roxy_browser_status"] = "open"
        _write(_ACCOUNTS, rows)
        return _public_account(row)


def mark_roxy_profile_deleted(account_id: int) -> dict | None:
    with _LOCK:
        rows = _read(_ACCOUNTS)
        row = next((item for item in rows if int(item.get("id") or 0) == int(account_id)), None)
        if not row or row.get("status") != "success":
            return None
        now = _now()
        row.update({
            "roxy_browser_status": "deleted",
            "roxy_closed_at": now,
            "roxy_deleted_at": now,
            "roxy_profile_id": "",
            "updated_at": now,
        })
        _write(_ACCOUNTS, rows)
        return _public_account(row)


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
        profile_id = str(result.get("roxy_profile_id") or "").strip()
        task.update({
            "status": "success", "stage": "kept_open",
            "message": "换绑完成；AT 已获取，Roxy 窗口保持新邮箱登录态",
            "completed_at": now, "updated_at": now, "verified_email": verified_email,
            "roxy_profile_id": profile_id, "roxy_browser_status": "open",
        })
        account.update({
            "status": "success", "current_email": verified_email, "new_email": verified_email,
            "replacement_api_url": str(replacement.get("api_url") or "").strip(),
            "access_token": access_token, "rebound_at": now, "at_refreshed_at": now,
            "at_saved_at": now,
            "at_refresh_status": "success", "roxy_profile_id": profile_id,
            "roxy_browser_status": "open", "updated_at": now,
        })
        account.pop("active_task_id", None)
        account.pop("error", None)
        replacement.update({
            "status": "used", "used_at": now, "updated_at": now,
            "bound_old_email": task.get("old_email"), "bound_account_id": account.get("id"),
        })
        replacement.pop("active_task_id", None)
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


def retry_transient_failure(task_id: int, error: str, max_retries: int) -> dict:
    """安全地重试流程早期临时故障，复用同一替换邮箱并建立任务链。"""
    with _LOCK:
        tasks = _read(_TASKS)
        accounts = _read(_ACCOUNTS)
        replacements = _read(_REPLACEMENTS)
        previous = next((row for row in tasks if int(row.get("id") or 0) == int(task_id)), None)
        if not previous:
            return {"next_task": None, "reason": "missing_task"}
        account = next((
            row for row in accounts
            if int(row.get("id") or 0) == int(previous.get("account_id") or 0)
        ), None)
        replacement = next((
            row for row in replacements
            if int(row.get("id") or 0) == int(previous.get("replacement_id") or 0)
        ), None)
        if not account or not replacement:
            return {"next_task": None, "reason": "missing_context"}

        now = _now()
        clean_error = str(error or "临时故障")[:600]
        retry_count = int(previous.get("transient_retry_count") or 0)
        limit = max(0, int(max_retries or 0))
        if retry_count >= limit:
            previous.update({
                "status": "failed", "stage": "failed", "message": clean_error,
                "error": clean_error, "retryable": True,
                "auto_retry_status": "exhausted", "completed_at": now, "updated_at": now,
            })
            account.update({"status": "failed", "error": clean_error, "updated_at": now})
            account.pop("active_task_id", None)
            replacement.update({"status": "available", "error": clean_error, "updated_at": now})
            replacement.pop("active_task_id", None)
            replacement.pop("bound_old_email", None)
            _write(_TASKS, tasks)
            _write(_ACCOUNTS, accounts)
            _write(_REPLACEMENTS, replacements)
            return {"next_task": None, "reason": "attempt_limit", "message": clean_error}

        next_attempt = int(previous.get("attempt") or 1) + 1
        next_count = retry_count + 1
        next_task = {
            "id": _next_id(tasks),
            "account_id": int(account.get("id") or 0),
            "replacement_id": int(replacement.get("id") or 0),
            "old_email": account.get("old_email"),
            "new_email": replacement.get("email"),
            "status": "queued", "stage": "auto_retry", "attempt": next_attempt,
            "root_task_id": int(previous.get("root_task_id") or previous.get("id") or 0),
            "retry_of_task_id": int(previous.get("id") or 0),
            "transient_retry_count": next_count,
            "message": f"第 {next_attempt} 次尝试：临时故障自动重试，继续使用当前替换邮箱",
            "created_at": now, "updated_at": now,
        }
        if previous.get("max_transient_retries") is not None:
            next_task["max_transient_retries"] = max(
                0, min(int(previous.get("max_transient_retries") or 0), 10)
            )
        previous.update({
            "status": "failed", "stage": "transient_failed", "message": clean_error,
            "error": clean_error, "retryable": True, "auto_retry_status": "scheduled",
            "next_task_id": next_task["id"], "completed_at": now, "updated_at": now,
        })
        account.update({"status": "queued", "active_task_id": next_task["id"], "updated_at": now})
        account.pop("error", None)
        replacement.update({
            "status": "reserved", "active_task_id": next_task["id"],
            "bound_old_email": account.get("old_email"), "updated_at": now,
        })
        replacement.pop("error", None)
        tasks.append(next_task)
        _write(_TASKS, tasks)
        _write(_ACCOUNTS, accounts)
        _write(_REPLACEMENTS, replacements)
        return {"next_task": dict(next_task), "reason": "scheduled"}


def recover_interrupted_tasks() -> int:
    active = [row for row in _read(_TASKS) if row.get("status") in {"queued", "running"}]
    uncertain_stages = {"submit_new_email_otp", "changed", "relogin_new", "verified", "kept_open"}
    for row in active:
        task_id = int(row.get("id") or 0)
        if str(row.get("stage") or "") in uncertain_stages:
            finish_review_failure(
                task_id,
                str(row.get("new_email") or ""),
                "分站在验证码提交/邮箱变更后重启，结果待人工核验，已冻结账号和替换邮箱",
            )
        else:
            finish_failure(task_id, "分站进程重启，原任务已释放，可重新开始")
    return len(active)


def recover_interrupted_access_token_refreshes() -> int:
    with _LOCK:
        rows = _read(_ACCOUNTS)
        recovered = 0
        now = _now()
        for row in rows:
            if row.get("at_refresh_status") != "running":
                continue
            row.update({
                "at_refresh_status": "failed",
                "at_refresh_error": "分站进程在重新获取 AT 时重启，请再次点击重新获取 AT",
                "updated_at": now,
            })
            recovered += 1
        if recovered:
            _write(_ACCOUNTS, rows)
        return recovered


def _replacement_api_url(row: dict, replacements: list[dict]) -> str:
    persisted = str(row.get("replacement_api_url") or "").strip()
    if persisted:
        return persisted
    account_id = int(row.get("id") or 0)
    new_email = str(row.get("new_email") or row.get("current_email") or "").strip().lower()
    replacement = next((
        item for item in replacements
        if int(item.get("bound_account_id") or 0) == account_id
    ), None)
    if replacement is None and new_email:
        replacement = next((
            item for item in replacements
            if str(item.get("email") or "").strip().lower() == new_email
        ), None)
    return str((replacement or {}).get("api_url") or "").strip()


def backfill_success_replacement_api_urls() -> int:
    """为升级前已成功账号补写替换邮箱 URL，避免号池记录删除后无法导出。"""
    with _LOCK:
        accounts = _read(_ACCOUNTS)
        replacements = _read(_REPLACEMENTS)
        changed = 0
        now = _now()
        for row in accounts:
            if row.get("status") != "success" or row.get("replacement_api_url"):
                continue
            api_url = _replacement_api_url(row, replacements)
            if not api_url:
                continue
            row["replacement_api_url"] = api_url
            row["updated_at"] = now
            changed += 1
        if changed:
            _write(_ACCOUNTS, accounts)
        return changed


def _export_success_line(row: dict, replacements: list[dict]) -> str | None:
    new_email = str(row.get("new_email") or row.get("current_email") or "").strip()
    access_token = str(row.get("access_token") or "").strip()
    password = str(row.get("password") or "").strip()
    totp_secret = str(row.get("totp_secret") or "").strip()
    old_email = str(row.get("old_email") or "").strip()
    source_api_url = str(row.get("api_url") or "").strip()
    if not old_email or not new_email or not access_token:
        return None
    if password and totp_secret:
        return "----".join([old_email, new_email, password, totp_secret, access_token])
    if source_api_url:
        replacement_api_url = _replacement_api_url(row, replacements)
        if replacement_api_url:
            return "----".join([old_email, new_email, replacement_api_url, access_token])
    return None


def export_success_line(account_id: int) -> str | None:
    with _LOCK:
        row = next((
            item for item in _read(_ACCOUNTS)
            if int(item.get("id") or 0) == int(account_id) and item.get("status") == "success"
        ), None)
        return _export_success_line(row, _read(_REPLACEMENTS)) if row else None


def export_success_lines() -> list[str]:
    with _LOCK:
        rows = [row for row in _read(_ACCOUNTS) if row.get("status") == "success"]
        rows.sort(key=lambda row: int(row.get("id") or 0))
        replacements = _read(_REPLACEMENTS)
        return [line for row in rows if (line := _export_success_line(row, replacements))]
