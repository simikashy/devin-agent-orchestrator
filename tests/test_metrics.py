from factories import make_task


def seed(store):
    store.insert_task("t1", make_task(repository="o/a", status="completed", created_at=100.0, updated_at=110.0, session_ended_at=110.0))
    store.insert_task("t2", make_task(repository="o/a", status="completed", created_at=200.0, updated_at=230.0, session_ended_at=230.0))
    store.insert_task("t3", make_task(repository="o/b", status="failed", created_at=300.0, updated_at=320.0, session_ended_at=320.0))
    store.insert_task("t4", make_task(repository="o/b", status="queued", created_at=400.0, updated_at=400.0))


def test_metrics_summary_and_mttr_split(client, store):
    seed(store)
    summary = client.get("/metrics").json()["summary"]
    assert summary["total_triggered_jobs"] == 4
    assert summary["completed"] == 2
    assert summary["failed"] == 1
    assert summary["queued"] == 1
    assert summary["mean_time_to_resolution_seconds"] == 20.0
    assert summary["mean_time_to_failure_seconds"] == 20.0


def test_metrics_repository_filter(client, store):
    seed(store)
    data = client.get("/metrics", params={"repository": "o/a"}).json()
    assert data["total"] == 2
    assert set(data["tasks"].keys()) == {"t1", "t2"}
    assert data["summary"]["total_triggered_jobs"] == 2


def test_metrics_status_filter_and_pagination(client, store):
    seed(store)
    data = client.get("/metrics", params={"status": "completed", "page": 1, "page_size": 1}).json()
    assert data["total"] == 2
    assert data["page_size"] == 1
    assert len(data["tasks"]) == 1


def test_metrics_time_window_filter(client, store):
    seed(store)
    data = client.get("/metrics", params={"from": 150.0, "to": 350.0}).json()
    assert set(data["tasks"].keys()) == {"t2", "t3"}
