import csv
import io

from factories import make_task

EXPECTED_HEADER = [
    "task_id",
    "issue_id",
    "repository",
    "status",
    "failure_category",
    "failure_reason",
    "pr_url",
    "created_at",
    "updated_at",
    "duration",
    "acu_used",
]


def parse_rows(text):
    return list(csv.reader(io.StringIO(text)))


def test_export_csv_contents(client, store):
    store.insert_task(
        "t1",
        make_task(
            repository="o/a",
            status="completed",
            created_at=100.0,
            updated_at=130.0,
            pr_url="https://example/pr/1",
            issue_id="11",
        ),
    )
    store.insert_task(
        "t2",
        make_task(
            repository="o/b",
            status="failed",
            created_at=200.0,
            updated_at=220.0,
            failure_category="code_bug",
            failure_reason="tests failed",
            issue_id="12",
        ),
    )
    response = client.get("/export/tasks.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert "tasks.csv" in response.headers["content-disposition"]

    rows = parse_rows(response.text)
    assert rows[0] == EXPECTED_HEADER
    by_id = {row[0]: row for row in rows[1:]}
    assert by_id["t1"][2] == "o/a"
    assert by_id["t1"][3] == "completed"
    assert by_id["t1"][6] == "https://example/pr/1"
    assert by_id["t1"][9] == "30.0"
    assert by_id["t2"][4] == "code_bug"
    assert by_id["t2"][5] == "tests failed"
    assert by_id["t2"][9] == "20.0"


def test_export_csv_repository_filter(client, store):
    store.insert_task("t1", make_task(repository="o/a", status="completed"))
    store.insert_task("t2", make_task(repository="o/b", status="failed"))
    response = client.get("/export/tasks.csv", params={"repository": "o/a"})
    rows = parse_rows(response.text)
    assert [row[0] for row in rows[1:]] == ["t1"]


def test_export_csv_requires_token_when_set(client, store, monkeypatch):
    monkeypatch.setenv("ASOC_DASHBOARD_TOKEN", "dash")
    assert client.get("/export/tasks.csv").status_code == 401
    assert client.get("/export/tasks.csv", headers={"Authorization": "Bearer dash"}).status_code == 200
