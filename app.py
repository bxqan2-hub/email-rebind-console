# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import quote

from flask import Flask, Response, jsonify, render_template, request

import settings
import roxy_flow
import store
import worker


def create_app(*, recover: bool = True) -> Flask:
    app = Flask(__name__)
    app.config.update(JSON_AS_ASCII=False, MAX_CONTENT_LENGTH=4 * 1024 * 1024)
    if recover:
        store.recover_interrupted_tasks()
        store.recover_interrupted_access_token_refreshes()
        store.backfill_success_replacement_api_urls()

    @app.get("/")
    def index():
        return render_template("index.html", port=settings.PORT)

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "email-rebind-console", "port": settings.PORT})

    @app.get("/api/state")
    def api_state():
        return jsonify({
            "ok": True,
            "summary": store.summary(),
            "accounts": store.list_accounts(),
            "replacements": store.list_replacements(),
            "proxies": store.list_proxies(),
            "tasks": store.list_tasks(),
            "pool_name": "替换邮箱",
            "proxy_pool_name": "换绑代理",
        })

    @app.post("/api/accounts/import")
    def api_import_accounts():
        data = request.get_json(silent=True) or {}
        result = store.import_source_accounts(str(data.get("text") or ""))
        if not result["parsed"]:
            return jsonify({
                "ok": False,
                "error": "未识别到原邮箱格式：邮箱----API取码URL，或 邮箱----密码----MFA Secret",
                **result,
            }), 400
        return jsonify({"ok": True, **result})

    @app.post("/api/replacements/import")
    def api_import_replacements():
        data = request.get_json(silent=True) or {}
        result = store.import_replacement_emails(str(data.get("text") or ""))
        if not result["parsed"]:
            return jsonify({"ok": False, "error": "未识别到替换邮箱格式：邮箱----API取码地址", **result}), 400
        return jsonify({"ok": True, **result})

    @app.post("/api/replacements/<int:replacement_id>/restore")
    def api_restore_replacement(replacement_id: int):
        row = store.restore_replacement(replacement_id)
        if row is None:
            return jsonify({"ok": False, "error": "替换邮箱不存在或当前不是失败状态"}), 409
        return jsonify({"ok": True, "item": row})

    @app.delete("/api/replacements/<int:replacement_id>")
    def api_delete_replacement(replacement_id: int):
        result = store.delete_replacement(replacement_id)
        if result.get("deleted"):
            return jsonify({"ok": True, **result})
        if result.get("reason") == "not_found":
            return jsonify({"ok": False, "error": "替换邮箱不存在", **result}), 404
        return jsonify({
            "ok": False,
            "error": f"替换邮箱正被任务 #{int(result.get('task_id') or 0)} 使用，请等待任务结束后再删除",
            **result,
        }), 409

    @app.delete("/api/accounts/<int:account_id>")
    def api_delete_account(account_id: int):
        result = store.delete_source_account(account_id)
        if result.get("deleted"):
            return jsonify({"ok": True, **result})
        if result.get("reason") == "not_found":
            return jsonify({"ok": False, "error": "原邮箱账号不存在", **result}), 404
        if result.get("reason") == "result_locked":
            return jsonify({
                "ok": False,
                "error": "待核验账号已锁定，请确认实际换绑状态后再处理",
                **result,
            }), 409
        if result.get("reason") == "window_open":
            return jsonify({
                "ok": False,
                "error": "请先关闭成功账号的 Roxy 窗口，再清理完成账号",
                **result,
            }), 409
        return jsonify({
            "ok": False,
            "error": f"原邮箱账号正被任务 #{int(result.get('task_id') or 0)} 使用，请等待任务结束后再删除",
            **result,
        }), 409

    @app.post("/api/proxies/import")
    def api_import_proxies():
        data = request.get_json(silent=True) or {}
        result = store.import_proxies(str(data.get("text") or ""))
        if not result["parsed"]:
            return jsonify({
                "ok": False,
                "error": "未识别到代理格式：URL、host:port、host:port:user:pass，或 host:port----用户名----密码",
                **result,
            }), 400
        return jsonify({"ok": True, **result})

    @app.post("/api/proxies/<int:proxy_id>/restore")
    def api_restore_proxy(proxy_id: int):
        row = store.restore_proxy(proxy_id)
        if row is None:
            return jsonify({"ok": False, "error": "代理不存在或当前不是失败状态"}), 409
        return jsonify({"ok": True, "item": row})

    @app.delete("/api/proxies/<int:proxy_id>")
    def api_delete_proxy(proxy_id: int):
        result = store.delete_proxy(proxy_id)
        if result.get("deleted"):
            return jsonify({"ok": True, **result})
        if result.get("reason") == "not_found":
            return jsonify({"ok": False, "error": "代理不存在", **result}), 404
        return jsonify({
            "ok": False,
            "error": f"代理正被任务 #{int(result.get('task_id') or 0)} 使用，请等待任务结束后再删除",
            **result,
        }), 409

    @app.delete("/api/proxies")
    def api_delete_all_proxies():
        result = store.delete_all_proxies()
        return jsonify({"ok": True, **result})

    @app.post("/api/accounts/<int:account_id>/refresh-at")
    def api_refresh_access_token(account_id: int):
        result = worker.submit_access_token_refresh(account_id)
        if not result.get("accepted"):
            return jsonify({"ok": False, **result}), 409
        return jsonify({"ok": True, **result}), 202


    @app.get("/api/accounts/<int:account_id>/access-token")
    def api_account_access_token(account_id: int):
        access_token = store.get_success_access_token(account_id)
        if not access_token:
            return jsonify({"ok": False, "error": "该成功账号暂无可复制的 AT"}), 404
        return Response(
            access_token,
            content_type="text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/accounts/<int:account_id>/close-roxy")
    def api_close_roxy(account_id: int):
        account = store.get_success_account_context(account_id)
        if not account:
            return jsonify({"ok": False, "error": "成功账号不存在"}), 404
        if account.get("at_refresh_status") == "running":
            return jsonify({"ok": False, "error": "该账号正在重新获取 AT，请完成后再关闭窗口"}), 409
        if account.get("roxy_browser_status") == "deleted":
            return jsonify({"ok": True, "already_deleted": True, "item": account})
        profile_id = str(account.get("roxy_profile_id") or "").strip()
        if not profile_id:
            return jsonify({"ok": False, "error": "账号没有可删除的 Roxy profile_id"}), 409
        try:
            roxy_flow_result = roxy_flow.delete_retained_profile(profile_id)
        except Exception as exc:
            logging.getLogger(__name__).exception("关闭并删除成功账号 Roxy Profile 失败 account=%s", account_id)
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}), 500
        item = store.mark_roxy_profile_deleted(account_id)
        return jsonify({"ok": True, "closed": True, "deleted": bool(roxy_flow_result), "item": item})

    @app.post("/api/pairs/preview")
    def api_preview():
        data = request.get_json(silent=True) or {}
        raw_ids = data.get("account_ids") or []
        if not isinstance(raw_ids, list):
            return jsonify({"ok": False, "error": "account_ids 必须是数组"}), 400
        ids = [int(value) for value in raw_ids if str(value).isdigit()]
        pairs = store.preview_pairs(ids)
        return jsonify({"ok": True, "pairs": pairs, "count": len(pairs)})

    @app.post("/api/rebind/start")
    def api_start():
        data = request.get_json(silent=True) or {}
        raw_ids = data.get("account_ids") or []
        if not isinstance(raw_ids, list):
            return jsonify({"ok": False, "error": "account_ids 必须是数组"}), 400
        ids = [int(value) for value in raw_ids if str(value).isdigit()]
        try:
            workers = max(1, min(int(data.get("workers") or settings.DEFAULT_WORKERS), 10))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是 1~10 的整数"}), 400
        if int(store.summary().get("proxy_available") or 0) < 1:
            return jsonify({"ok": False, "error": "换绑代理池没有可用代理，请先手动导入"}), 409
        tasks = store.reserve_batch(ids)
        if not tasks:
            return jsonify({"ok": False, "error": "没有可一对一配对的待换绑账号和替换邮箱"}), 409
        submitted = worker.submit_tasks(tasks, workers)
        return jsonify({"ok": True, "submitted": submitted, "workers": workers, "tasks": tasks})

    @app.post("/api/accounts/<int:account_id>/retry")
    def api_retry_failed_account(account_id: int):
        if int(store.summary().get("proxy_available") or 0) < 1:
            return jsonify({"ok": False, "error": "换绑代理池没有可用代理，请先重新启用或导入代理"}), 409
        result = store.reserve_failed_account_retry(account_id)
        task = result.get("task")
        if not task:
            reason = result.get("reason")
            if reason == "not_found":
                return jsonify({"ok": False, "error": "原邮箱账号不存在"}), 404
            if reason == "not_failed":
                return jsonify({"ok": False, "error": "只有失败账号可以使用失败重试"}), 409
            if reason == "busy":
                return jsonify({"ok": False, "error": "该账号已有活动换绑任务"}), 409
            return jsonify({"ok": False, "error": "替换邮箱号池没有可用邮箱"}), 409
        submitted = worker.submit_tasks([task], 1)
        return jsonify({"ok": True, "submitted": submitted, "task": task}), 202

    @app.get("/api/tasks")
    def api_tasks():
        return jsonify({"ok": True, "items": store.list_tasks(), "summary": store.summary()})

    @app.delete("/api/tasks/failed")
    def api_clear_failed_tasks():
        result = store.clear_failed_tasks()
        return jsonify({"ok": True, **result})

    @app.delete("/api/tasks/finished")
    def api_clear_finished_tasks():
        result = store.clear_finished_tasks()
        return jsonify({"ok": True, **result})

    @app.delete("/api/tasks/<int:task_id>")
    def api_delete_finished_task(task_id: int):
        result = store.delete_finished_task(task_id)
        if result.get("deleted"):
            return jsonify({"ok": True, **result})
        if result.get("reason") == "not_found":
            return jsonify({"ok": False, "error": "任务日志不存在", **result}), 404
        return jsonify({"ok": False, "error": "只能清理已结束的成功、失败或待核验任务日志", **result}), 409

    def export_response(lines: list[str], *, account_id: int | None = None) -> Response:
        body = "\n".join(lines) + ("\n" if lines else "")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        readable = f"换绑完成-账号{account_id}-{stamp}.txt" if account_id else f"换绑完成-{stamp}.txt"
        fallback = f"rebind-account-{account_id}-{stamp}.txt" if account_id else f"rebind-results-{stamp}.txt"
        return Response(
            body,
            content_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(readable)}",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/export")
    def api_export():
        return export_response(store.export_success_lines())

    @app.get("/api/accounts/<int:account_id>/export")
    def api_export_account(account_id: int):
        line = store.export_success_line(account_id)
        if line is None:
            return jsonify({"ok": False, "error": "该成功账号暂无完整导出结果"}), 404
        return export_response([line], account_id=account_id)

    return app


def _configure_logging() -> None:
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(settings.LOG_DIR / "rebind.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    # 页面会按固定间隔读取任务状态；正常的 200/304 访问日志不应刷屏。
    # WARNING/ERROR 仍写入 logs/rebind.log，方便排查启动和任务异常。
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


if __name__ == "__main__":
    _configure_logging()
    create_app().run(host=settings.HOST, port=settings.PORT, threaded=True, use_reloader=False)
