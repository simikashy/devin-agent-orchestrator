from factories import make_task
from storage import TaskStore


def test_insert_and_load_round_trip(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    store.insert_task("task_1", make_task(repository="acme/api", status="completed", issue_id="42"))
    tasks = store.load_tasks()
    assert "task_1" in tasks
    assert tasks["task_1"]["repository"] == "acme/api"
    assert tasks["task_1"]["status"] == "completed"
    assert store.get_task("task_1")["issue_id"] == "42"
    assert store.count() == 1


def test_update_task_round_trip(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    store.insert_task("task_1", make_task(status="queued"))
    updated = store.update_task("task_1", status="completed", pr_url="https://example/pr/1")
    assert updated["status"] == "completed"
    assert updated["pr_url"] == "https://example/pr/1"
    assert updated["updated_at"] >= updated["created_at"]
    assert store.update_task("missing", status="completed") is None


def test_query_filters(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    store.insert_task("a", make_task(repository="o/a", status="completed", created_at=100.0))
    store.insert_task("b", make_task(repository="o/b", status="failed", created_at=200.0))
    store.insert_task("c", make_task(repository="o/a", status="queued", created_at=300.0))
    assert {tid for tid, _ in store.query_tasks(repository="o/a")} == {"a", "c"}
    assert [tid for tid, _ in store.query_tasks(status="failed")] == ["b"]
    window = store.query_tasks(time_from=150.0, time_to=250.0)
    assert [tid for tid, _ in window] == ["b"]


def test_query_orders_by_created_desc(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    store.insert_task("old", make_task(created_at=100.0))
    store.insert_task("new", make_task(created_at=300.0))
    store.insert_task("mid", make_task(created_at=200.0))
    assert [tid for tid, _ in store.query_tasks()] == ["new", "mid", "old"]


def test_delete_task_round_trip(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    store.insert_task("a", make_task())
    assert store.delete_task("a") is True
    assert store.get_task("a") is None
    assert store.count() == 0
    assert store.delete_task("a") is False


def test_find_in_flight(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    store.insert_task("a", make_task(repository="o/a", issue_id="7", status="running"))
    assert store.find_in_flight("o/a", "7", ("queued", "running")) == "a"
    assert store.find_in_flight("o/a", "7", ("queued",)) is None
    assert store.find_in_flight("o/a", "8", ("queued", "running")) is None


def test_ping(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    assert store.ping() is True


def test_session_url_round_trip(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    store.insert_task("a", make_task(session_url="https://app.devin.ai/sessions/abc"))
    assert store.get_task("a")["session_url"] == "https://app.devin.ai/sessions/abc"
    store.update_task("a", session_url="https://app.devin.ai/sessions/xyz")
    assert store.get_task("a")["session_url"] == "https://app.devin.ai/sessions/xyz"


def test_additive_migration_adds_session_url_to_legacy_db(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            issue_id TEXT,
            title TEXT,
            repository TEXT,
            branch TEXT,
            status TEXT,
            session_id TEXT,
            pr_url TEXT,
            failure_category TEXT,
            failure_reason TEXT,
            created_at REAL,
            updated_at REAL,
            error TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO tasks (id, issue_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("old_task", "99", "completed", 100.0, 200.0),
    )
    conn.commit()
    conn.close()

    store = TaskStore(db_path)

    task = store.get_task("old_task")
    assert task is not None
    assert task["session_url"] is None
    assert task["status"] == "completed"

    store.update_task("old_task", session_url="https://app.devin.ai/sessions/migrated")
    assert store.get_task("old_task")["session_url"] == "https://app.devin.ai/sessions/migrated"
