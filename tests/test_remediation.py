import main
from factories import make_task


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    @property
    def ok(self):
        return self.status_code < 400

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


def test_run_remediation_success_with_200_status(store, monkeypatch):
    store.insert_task("task_200", make_task(status="queued"))
    monkeypatch.setattr(main, "post_issue_comment", lambda *a, **k: None)
    monkeypatch.setattr(main, "create_devin_session", lambda p: FakeResponse(200, {"session_id": "sess_200"}))
    monkeypatch.setattr(
        main,
        "get_devin_session",
        lambda sid: FakeResponse(
            200,
            {
                "status_enum": "finished",
                "structured_output": {"result": "success"},
                "pull_request": {"url": "https://example/pr/200"},
            },
        ),
    )
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    main.run_devin_remediation("task_200", payload())

    updated = store.get_task("task_200")
    assert updated["status"] == "completed"
    assert updated["session_id"] == "sess_200"
    assert updated["pr_url"] == "https://example/pr/200"


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


def test_parse_github_pr_url():
    assert main.parse_github_pr_url("https://github.com/octo/repo/pull/42") == ("octo", "repo", "42")
    assert main.parse_github_pr_url("https://github.com/octo/repo/pull/abc") is None
    assert main.parse_github_pr_url("https://github.com/octo/repo/issues/42") is None
    assert main.parse_github_pr_url("not-a-url") is None
    assert main.parse_github_pr_url("") is None
    assert main.parse_github_pr_url(None) is None


def test_blocked_with_pr_transitions_to_pr_status(store, monkeypatch):
    store.insert_task("task_pr", make_task(status="running", session_id="sess_pr"))
    comments = []
    tracked = []
    monkeypatch.setattr(main, "post_issue_comment", lambda repo, issue, body: comments.append(body))
    monkeypatch.setattr(main, "start_pr_tracking_thread", lambda tid, p, url: tracked.append((tid, url)))

    data = {
        "status_enum": "blocked",
        "pull_request": {"url": "https://github.com/octo/repo/pull/42"},
    }
    main.finalize_from_session("task_pr", payload(), data)

    updated = store.get_task("task_pr")
    assert updated["status"] == "PR"
    assert updated["pr_url"] == "https://github.com/octo/repo/pull/42"
    assert updated["failure_category"] is None
    assert tracked == [("task_pr", "https://github.com/octo/repo/pull/42")]
    assert comments


def test_blocked_without_pr_fails(store, monkeypatch):
    store.insert_task("task_block", make_task(status="running", session_id="sess_block"))
    monkeypatch.setattr(main, "post_issue_comment", lambda *a, **k: None)
    monkeypatch.setattr(main, "start_pr_tracking_thread", lambda *a, **k: None)

    data = {"status_enum": "blocked"}
    main.finalize_from_session("task_block", payload(), data)

    updated = store.get_task("task_block")
    assert updated["status"] == "failed"
    assert "blocked" in updated["failure_reason"]


def test_poll_pr_for_merge_completes_on_merge(store, monkeypatch):
    store.insert_task("task_merge", make_task(status="PR", pr_url="https://github.com/octo/repo/pull/42"))
    comments = []
    closed = []
    monkeypatch.setattr(main, "post_issue_comment", lambda repo, issue, body: comments.append(body))
    monkeypatch.setattr(main, "close_github_issue", lambda repo, issue: closed.append((repo, issue)))
    monkeypatch.setattr(
        main,
        "get_github_pull_request",
        lambda owner, repo, number: FakeResponse(200, {"merged": True, "state": "closed"}),
    )
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    main.poll_pr_for_merge("task_merge", payload(), "https://github.com/octo/repo/pull/42")

    updated = store.get_task("task_merge")
    assert updated["status"] == "completed"
    assert updated["pr_url"] == "https://github.com/octo/repo/pull/42"
    assert closed == [("octo/repo", "1")]
    assert any("Merged Pull Request" in c for c in comments)


def test_poll_pr_for_merge_fails_when_closed_unmerged(store, monkeypatch):
    store.insert_task("task_closed", make_task(status="PR", pr_url="https://github.com/octo/repo/pull/7"))
    monkeypatch.setattr(main, "post_issue_comment", lambda *a, **k: None)
    monkeypatch.setattr(main, "close_github_issue", lambda *a, **k: None)
    monkeypatch.setattr(
        main,
        "get_github_pull_request",
        lambda owner, repo, number: FakeResponse(200, {"merged": False, "state": "closed"}),
    )
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    main.poll_pr_for_merge("task_closed", payload(), "https://github.com/octo/repo/pull/7")

    updated = store.get_task("task_closed")
    assert updated["status"] == "failed"
    assert updated["failure_category"] == "configuration"


def test_recover_resumes_pr_tracking_for_pr_tasks(store, monkeypatch):
    store.insert_task(
        "task_recover",
        make_task(status="PR", session_id="sess_r", pr_url="https://github.com/octo/repo/pull/99"),
    )
    tracked = []
    monkeypatch.setattr(main, "start_pr_tracking_thread", lambda tid, p, url: tracked.append((tid, url)))

    main.recover_in_flight_tasks()

    assert tracked == [("task_recover", "https://github.com/octo/repo/pull/99")]
    assert store.get_task("task_recover")["status"] == "PR"


def test_run_remediation_persists_session_url(store, monkeypatch):
    store.insert_task("task_url", make_task(status="queued"))
    monkeypatch.setattr(main, "post_issue_comment", lambda *a, **k: None)
    monkeypatch.setattr(
        main,
        "create_devin_session",
        lambda p: FakeResponse(
            200,
            {
                "session_id": "sess_url_1",
                "url": "https://app.devin.ai/sessions/sess_url_1",
            },
        ),
    )
    monkeypatch.setattr(
        main,
        "get_devin_session",
        lambda sid: FakeResponse(
            200,
            {
                "status_enum": "finished",
                "structured_output": {"result": "success"},
                "pull_request": {"url": "https://example/pr/url-test"},
            },
        ),
    )
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    main.run_devin_remediation("task_url", payload())

    updated = store.get_task("task_url")
    assert updated["session_url"] == "https://app.devin.ai/sessions/sess_url_1"
    assert updated["session_id"] == "sess_url_1"


def test_run_remediation_handles_missing_session_url(store, monkeypatch):
    store.insert_task("task_nourl", make_task(status="queued"))
    monkeypatch.setattr(main, "post_issue_comment", lambda *a, **k: None)
    monkeypatch.setattr(
        main,
        "create_devin_session",
        lambda p: FakeResponse(200, {"session_id": "sess_nourl"}),
    )
    monkeypatch.setattr(
        main,
        "get_devin_session",
        lambda sid: FakeResponse(
            200,
            {
                "status_enum": "finished",
                "structured_output": {"result": "success"},
                "pull_request": {"url": "https://example/pr/nourl"},
            },
        ),
    )
    monkeypatch.setattr(main.time, "sleep", lambda seconds: None)

    main.run_devin_remediation("task_nourl", payload())

    updated = store.get_task("task_nourl")
    assert updated["session_url"] is None
    assert updated["session_id"] == "sess_nourl"


def test_build_session_body_contains_schema_and_metadata():
    p = payload()
    body = main.build_session_body(p)

    assert "prompt" in body
    assert body["idempotent"] is True
    assert body["title"] == "ASOC remediation: Broken"
    assert body["tags"] == ["asoc", "issue-1", "octo/repo"]

    schema = body["structured_output_schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"result", "failure_category", "failure_reason", "pull_request_url"}
    assert schema["additionalProperties"] is False
    assert schema["properties"]["result"]["enum"] == ["success", "failure"]

    assert "max_acu_limit" not in body


def test_build_session_body_includes_max_acu_limit(monkeypatch):
    monkeypatch.setenv("ASOC_MAX_ACU_PER_SESSION", "10")
    p = payload()
    body = main.build_session_body(p)
    assert body["max_acu_limit"] == 10


def test_resolve_max_acu_limit_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("ASOC_MAX_ACU_PER_SESSION", raising=False)
    assert main.resolve_max_acu_limit() is None


def test_resolve_max_acu_limit_returns_none_for_invalid(monkeypatch):
    monkeypatch.setenv("ASOC_MAX_ACU_PER_SESSION", "abc")
    assert main.resolve_max_acu_limit() is None
    monkeypatch.setenv("ASOC_MAX_ACU_PER_SESSION", "0")
    assert main.resolve_max_acu_limit() is None
    monkeypatch.setenv("ASOC_MAX_ACU_PER_SESSION", "-5")
    assert main.resolve_max_acu_limit() is None


def test_resolve_max_acu_limit_returns_value(monkeypatch):
    monkeypatch.setenv("ASOC_MAX_ACU_PER_SESSION", "25")
    assert main.resolve_max_acu_limit() == 25
