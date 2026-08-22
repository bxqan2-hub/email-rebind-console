# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

import settings
import store
import worker


def create_app(*, recover: bool = True) -> Flask:
    app = Flask(__name__)
    app.config.update(JSON_AS_ASCII=False, MAX_CONTENT_LENGTH=4 * 1024 * 1024)
    if recover:
        store.recover_interrupted_tasks()

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
            "tasks": store.list_tasks(),
            "pool_name": "替换邮箱",
        })

    @app.post("/api/accounts/import")
    def api_import_accounts():
        data = request.get_json(silent=True) or {}
        result = store.import_source_accounts(str(data.get("text") or ""))
        if not result["parsed"]:
            return jsonify({"ok": False, "error": "未识别到主站格式：邮箱----密码----MFA Secret", **result}), 400
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
        tasks = store.reserve_batch(ids)
        if not tasks:
            return jsonify({"ok": False, "error": "没有可一对一配对的待换绑账号和替换邮箱"}), 409
        submitted = worker.submit_tasks(tasks, workers)
        return jsonify({"ok": True, "submitted": submitted, "workers": workers, "tasks": tasks})

    @app.get("/api/tasks")
    def api_tasks():
        return jsonify({"ok": True, "items": store.list_tasks(), "summary": store.summary()})

    @app.get("/api/export")
    def api_export():
        lines = store.export_success_lines()
        body = "\n".join(lines) + ("\n" if lines else "")
        filename = f"换绑完成-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        return Response(
            body,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )

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
