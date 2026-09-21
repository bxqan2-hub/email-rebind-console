import threading
from unittest.mock import Mock

import pytest

import app
import settings
import store
import worker


@pytest.fixture
def client(tmp_path, monkeypatch):
    for name in ("_ACCOUNTS", "_TASKS", "_REPLACEMENTS", "_PROXIES", "_DETECTION_PROXIES"):
        monkeypatch.setattr(store, name, tmp_path / f"{name}.json")
    monkeypatch.setattr(settings, "MAX_WORKERS", 100)
    monkeypatch.setattr(worker, "_EXECUTORS", [])
    store.import_source_accounts("\n".join(
        f"old{i}@example.com----Password!----JBSWY3DPEHPK3PXP" for i in range(40)
    ))
    store.import_replacement_emails("\n".join(
        f"new{i}@example.com----https://mail.example/{i}" for i in range(40)
    ))
    store.import_proxies("http://proxy.example:8080")
    return app.create_app(recover=False).test_client()


@pytest.mark.parametrize("mode", ["protocol", "browser"])
def test_request_for_36_workers_starts_36_concurrent_tasks(client, monkeypatch, mode):
    condition = threading.Condition()
    release = threading.Event()
    entered = []

    def run(task_id):
        store.update_task(task_id, status="running", stage="login_old")
        with condition:
            entered.append(task_id)
            condition.notify_all()
        release.wait(15)

    monkeypatch.setattr(worker, "_run", run)
    try:
        response = client.post("/api/rebind/start", json={"workers": 36, "rebind_mode": mode})
        assert response.status_code == 200
        assert response.json["workers"] == response.json["requested_workers"] == 36
        assert response.json["submitted"] == 40
        with condition:
            assert condition.wait_for(lambda: len(entered) >= 36, timeout=10)
            assert len(entered) == 36
        state = client.get("/api/state").json
        assert state["summary"]["tasks_running"] == 36
        assert state["summary"]["tasks_queued"] == 4
    finally:
        with worker._LOCK:
            executors = list(worker._EXECUTORS)
        release.set()
        for executor in executors:
            executor.shutdown(wait=True)
    assert len(entered) == len(set(entered)) == 40


@pytest.mark.parametrize("workers", [0, -1, 101, 36.5, True, None, "abc"])
def test_invalid_concurrency_is_rejected_before_reserving_accounts(client, monkeypatch, workers):
    submit = Mock()
    monkeypatch.setattr(worker, "submit_tasks", submit)
    response = client.post("/api/rebind/start", json={"workers": workers})
    assert response.status_code == 400
    assert "1~100" in response.json["error"]
    assert store.list_tasks() == []
    assert store.summary()["accounts_ready"] == 40
    submit.assert_not_called()


def test_effective_concurrency_uses_available_task_count(client, monkeypatch):
    submit = Mock(return_value=2)
    monkeypatch.setattr(worker, "submit_tasks", submit)
    response = client.post("/api/rebind/start", json={"workers": 36, "account_ids": [1, 2]})
    assert response.status_code == 200
    assert response.json["requested_workers"] == 36
    assert response.json["workers"] == 2
    assert submit.call_args.args[1] == 2


def test_page_and_api_use_same_configured_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "MAX_WORKERS", 72)
    assert client.get("/api/state").json["settings"]["max_workers"] == 72
    assert 'id="workers" type="number" min="1" max="72"' in client.get("/").text
    response = client.post("/api/rebind/start", json={"workers": 73})
    assert response.status_code == 400
    assert "1~72" in response.json["error"]
    with pytest.raises(ValueError, match="1~72"):
        worker.submit_tasks([{"id": 1}], 73)
