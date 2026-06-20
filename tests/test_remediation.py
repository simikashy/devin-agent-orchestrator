import main
from factories import make_task


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def payload():
    return main.RemediationRequest(
        issue_id="1",
        title="Broken",
        description="Something failed.",
        repository="octo/repo",
    )


def test_run_remediation_success(store, monkeypatch):
    store.insert_task("task_x", make_task(status="queued"))
    comments = []
    monkeypatch.setattr(main, "post_issue_comment", lambda repo, issue, body: comments.append(body))
    monkeypatch.setattr(main, "create_devin_session", lambda p: FakeResponse(201, {"session_id": "sess_1"}))
    monkeypatch.setattr(
        main,
        "get_devin_session",
        lambda sid: FakeResponse(
            200,
            {
                "status_enum": "finished",
                "structured_output": {"result": "success"},
                "pull_request": {"url": "https://example/pr/9"},
            },
        ),
    )
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    main.run_devin_remediation("task_x", payload())

    updated = store.get_task("task_x")
    assert updated["status"] == "completed"
    assert updated["pr_url"] == "https://example/pr/9"
    assert comments


def test_run_remediation_session_reported_failure(store, monkeypatch):
    store.insert_task("task_y", make_task(status="queued"))
    monkeypatch.setattr(main, "post_issue_comment", lambda *a, **k: None)
    monkeypatch.setattr(main, "create_devin_session", lambda p: FakeResponse(201, {"session_id": "sess_2"}))
    monkeypatch.setattr(
        main,
        "get_devin_session",
        lambda sid: FakeResponse(
            200,
            {
                "status_enum": "blocked",
                "structured_output": {
                    "result": "failure",
                    "failure_category": "test_failure",
                    "failure_reason": "Unit tests did not pass.",
                },
            },
        ),
    )
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    main.run_devin_remediation("task_y", payload())

    updated = store.get_task("task_y")
    assert updated["status"] == "failed"
    assert updated["failure_category"] == "test_failure"
    assert updated["failure_reason"] == "Unit tests did not pass."


def test_run_remediation_devin_api_error(store, monkeypatch):
    store.insert_task("task_z", make_task(status="queued"))
    monkeypatch.setattr(main, "post_issue_comment", lambda *a, **k: None)
    monkeypatch.setattr(main, "create_devin_session", lambda p: FakeResponse(500))
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    main.run_devin_remediation("task_z", payload())

    updated = store.get_task("task_z")
    assert updated["status"] == "failed"
    assert updated["failure_category"] == "configuration"
