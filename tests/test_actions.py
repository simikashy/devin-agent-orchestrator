from factories import make_task


def test_retry_failed_task(client, store):
    store.insert_task("f1", make_task(status="failed", repository="o/r", issue_id="9"))
    response = client.post("/tasks/f1/retry")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert data["source_task_id"] == "f1"
    assert data["task_id"] != "f1"


def test_retry_non_failed_conflict(client, store):
    store.insert_task("q1", make_task(status="queued"))
    assert client.post("/tasks/q1/retry").status_code == 409


def test_retry_unknown_task_not_found(client, store):
    assert client.post("/tasks/missing/retry").status_code == 404


def test_cancel_queued_task(client, store):
    store.insert_task("q1", make_task(status="queued"))
    response = client.post("/tasks/q1/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert store.get_task("q1")["status"] == "cancelled"


def test_cancel_terminal_conflict(client, store):
    store.insert_task("c1", make_task(status="completed"))
    assert client.post("/tasks/c1/cancel").status_code == 409


def test_cancel_unknown_task_not_found(client, store):
    assert client.post("/tasks/missing/cancel").status_code == 404


def test_remove_failed_task_deletes_record(client, store):
    store.insert_task("rm1", make_task(status="failed", repository="o/r", issue_id="9"))
    response = client.request("DELETE", "/tasks/rm1")
    assert response.status_code == 200
    assert response.json() == {"status": "removed", "task_id": "rm1"}
    assert store.get_task("rm1") is None


def test_remove_unknown_task_not_found(client, store):
    assert client.request("DELETE", "/tasks/missing").status_code == 404


def test_removed_task_excluded_from_metrics(client, store):
    store.insert_task("keep", make_task(status="completed"))
    store.insert_task("gone", make_task(status="failed"))
    assert client.request("DELETE", "/tasks/gone").status_code == 200
    summary = client.get("/metrics").json()["summary"]
    assert summary["total_triggered_jobs"] == 1
    assert summary["failed"] == 0
    assert summary["completed"] == 1
