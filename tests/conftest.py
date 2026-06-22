import pytest
from fastapi.testclient import TestClient

import main
from storage import TaskStore

ISOLATED_ENV_VARS = (
    "ASOC_API_TOKEN",
    "ASOC_DASHBOARD_TOKEN",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
    "DEVIN_API_KEY",
    "ASOC_ACU_PER_MINUTE",
)


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    for name in ISOLATED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    def blocked(*args, **kwargs):
        raise AssertionError("Unexpected outbound network call during tests.")

    monkeypatch.setattr(main.requests, "post", blocked)
    monkeypatch.setattr(main.requests, "get", blocked)


@pytest.fixture
def store(tmp_path, monkeypatch):
    test_store = TaskStore(tmp_path / "tasks.db")
    monkeypatch.setattr(main, "store", test_store)
    return test_store


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(main.scheduler, "submit", lambda task_id, payload: None)
    return TestClient(main.app)
