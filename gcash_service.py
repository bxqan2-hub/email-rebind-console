# -*- coding: utf-8 -*-
"""Run the vendored MK GCash Link workspace beside the rebind console."""
from __future__ import annotations

import atexit
import importlib.util
import os
import sys
import threading
from pathlib import Path

import settings


UPSTREAM_ROOT = Path(__file__).resolve().parent / "integrations" / "mk_gcash_link"
UPSTREAM_APP = UPSTREAM_ROOT / "app.py"
UPSTREAM_COMMIT = "2607d879ce2005ef9a9c6cdfa1ec747c6f26d4d5"

_lock = threading.Lock()
_server = None
_thread: threading.Thread | None = None


def _load_upstream_app():
    if not UPSTREAM_APP.is_file():
        raise RuntimeError(f"GCash 上游入口不存在：{UPSTREAM_APP}")
    root = str(UPSTREAM_ROOT)
    os.environ.setdefault(
        "MK_EMBED_ORIGIN",
        f"http://{settings.GCASH_BROWSER_HOST}:{settings.PORT}",
    )
    if root not in sys.path:
        sys.path.insert(0, root)
    name = "email_rebind_vendored_mk_gcash_app"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, UPSTREAM_APP)
    if spec is None or spec.loader is None:
        raise RuntimeError("GCash 上游模块加载失败")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def start() -> dict:
    """Bind and start the vendored service once in a daemon thread."""
    global _server, _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return status()
        upstream = _load_upstream_app()
        _server = upstream.Server((settings.GCASH_HOST, settings.GCASH_PORT), upstream.Handler)
        _thread = threading.Thread(
            target=_server.serve_forever,
            name="mk-gcash-link",
            daemon=True,
        )
        _thread.start()
        return status()


def stop() -> None:
    global _server, _thread
    with _lock:
        server, thread = _server, _thread
        _server = None
        _thread = None
    if server is not None:
        server.shutdown()
        server.server_close()
    if thread is not None and thread.is_alive():
        thread.join(timeout=3)


def status() -> dict:
    running = bool(_thread is not None and _thread.is_alive())
    return {
        "running": running,
        "host": settings.GCASH_HOST,
        "port": settings.GCASH_PORT,
        "url": settings.GCASH_URL,
        "upstream_commit": UPSTREAM_COMMIT,
    }


atexit.register(stop)
