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


def test_extract_acu_used():
    assert main.extract_acu_used({"total_acu_used": 5.3}) == 5.3
    assert main.extract_acu_used({"total_acu_used": 0}) == 0.0
    assert main.extract_acu_used({"total_acu_used": 10}) == 10.0
    assert main.extract_acu_used({}) is None
    assert main.extract_acu_used({"total_acu_used": -1}) is None
    assert main.extract_acu_used({"total_acu_used": "bad"}) is None


def test_finalize_from_session_persists_acu_used(store, monkeypatch):
    store.insert_task("task_acu", make_task(status="running"))
    monkeypatch.setattr(main, "post_issue_comment", lambda *a, **k: None)

    data = {
        "status_enum": "finished",
        "structured_output": {"result": "success"},
        "pull_request": {"url": "https://example/pr/acu"},
        "total_acu_used": 7.2,
    }
    main.finalize_from_session("task_acu", payload(), data)

    updated = store.get_task("task_acu")
    assert updated["status"] == "completed"
    assert updated["acu_used"] == 7.2


def test_finalize_from_session_acu_used_on_failure(store, monkeypatch):
    store.insert_task("task_acu_fail", make_task(status="running"))
    monkeypatch.setattr(main, "post_issue_comment", lambda *a, **k: None)

    data = {
        "status_enum": "finished",
        "structured_output": {
            "result": "failure",
            "failure_category": "code_bug",
            "failure_reason": "Could not fix",
        },
        "total_acu_used": 3.1,
    }
    main.finalize_from_session("task_acu_fail", payload(), data)

    updated = store.get_task("task_acu_fail")
    assert updated["status"] == "failed"
    assert updated["acu_used"] == 3.1


def test_mark_task_failed_without_acu_preserves_existing(store):
    store.insert_task("task_preserve", make_task(status="running", acu_used=5.5))
    main.mark_task_failed("task_preserve", "PR closed", "configuration")
    updated = store.get_task("task_preserve")
    assert updated["status"] == "failed"
    assert updated["acu_used"] == 5.5


def test_csv_export_includes_acu_used():
    task = make_task(status="completed", acu_used=9.5)
    row = main.csv_row_for_task("t1", task)
    assert "acu_used" in main.CSV_EXPORT_HEADERS
    acu_index = list(main.CSV_EXPORT_HEADERS).index("acu_used")
    assert row[acu_index] == 9.5


def test_csv_export_acu_used_none_renders_empty():
    task = make_task(status="completed")
    row = main.csv_row_for_task("t1", task)
    acu_index = list(main.CSV_EXPORT_HEADERS).index("acu_used")
    assert row[acu_index] == ""


def test_resolve_acu_period_budget_unset(monkeypatch):
    monkeypatch.delenv("ASOC_ACU_PERIOD_BUDGET", raising=False)
    assert main.resolve_acu_period_budget() is None


def test_resolve_acu_period_budget_valid(monkeypatch):
    monkeypatch.setenv("ASOC_ACU_PERIOD_BUDGET", "5000")
    assert main.resolve_acu_period_budget() == 5000


def test_resolve_acu_period_budget_invalid(monkeypatch):
    monkeypatch.setenv("ASOC_ACU_PERIOD_BUDGET", "abc")
    assert main.resolve_acu_period_budget() is None
    monkeypatch.setenv("ASOC_ACU_PERIOD_BUDGET", "0")
    assert main.resolve_acu_period_budget() is None


def test_resolve_acu_period_days_default(monkeypatch):
    monkeypatch.delenv("ASOC_ACU_PERIOD_DAYS", raising=False)
    assert main.resolve_acu_period_days() == 30


def test_resolve_acu_period_days_valid(monkeypatch):
    monkeypatch.setenv("ASOC_ACU_PERIOD_DAYS", "7")
    assert main.resolve_acu_period_days() == 7


def test_resolve_acu_period_days_invalid(monkeypatch):
    monkeypatch.setenv("ASOC_ACU_PERIOD_DAYS", "bad")
    assert main.resolve_acu_period_days() == 30


def test_resolve_sweep_enabled_defaults_false(monkeypatch):
    monkeypatch.delenv("ASOC_SWEEP_ENABLED", raising=False)
    assert main.resolve_sweep_enabled() is False


def test_resolve_sweep_enabled_true(monkeypatch):
    monkeypatch.setenv("ASOC_SWEEP_ENABLED", "true")
    assert main.resolve_sweep_enabled() is True
    monkeypatch.setenv("ASOC_SWEEP_ENABLED", "1")
    assert main.resolve_sweep_enabled() is True


def test_resolve_sweep_interval_default(monkeypatch):
    monkeypatch.delenv("ASOC_SWEEP_INTERVAL_SECONDS", raising=False)
    assert main.resolve_sweep_interval() == 300


def test_resolve_sweep_interval_custom(monkeypatch):
    monkeypatch.setenv("ASOC_SWEEP_INTERVAL_SECONDS", "60")
    assert main.resolve_sweep_interval() == 60


def test_resolve_sweep_interval_minimum(monkeypatch):
    monkeypatch.setenv("ASOC_SWEEP_INTERVAL_SECONDS", "5")
    assert main.resolve_sweep_interval() == 300


def test_resolve_sweep_repos(monkeypatch):
    monkeypatch.setenv("ASOC_SWEEP_REPOS", "owner/a, owner/b")
    assert main.resolve_sweep_repos() == ["owner/a", "owner/b"]


def test_resolve_sweep_repos_empty(monkeypatch):
    monkeypatch.delenv("ASOC_SWEEP_REPOS", raising=False)
    assert main.resolve_sweep_repos() == []


def test_resolve_sweep_label_default(monkeypatch):
    monkeypatch.delenv("ASOC_SWEEP_LABEL", raising=False)
    assert main.resolve_sweep_label() == "trigger-devin"


def test_resolve_sweep_label_custom(monkeypatch):
    monkeypatch.setenv("ASOC_SWEEP_LABEL", "auto-fix")
    assert main.resolve_sweep_label() == "auto-fix"


def test_run_sweep_enqueues_new_issues(store, monkeypatch):
    issues = [
        {"number": 10, "title": "Bug A", "body": "desc A"},
        {"number": 20, "title": "Bug B", "body": "desc B"},
    ]
    monkeypatch.setenv("ASOC_SWEEP_REPOS", "octo/repo")
    monkeypatch.setattr(main, "list_labeled_issues", lambda repo, label: issues)

    result = main.run_sweep()
    assert result["enqueued"] == 2
    assert result["skipped"] == 0
    assert store.count() == 2


def test_run_sweep_deduplicates_existing(store, monkeypatch):
    store.insert_task("existing", make_task(repository="octo/repo", issue_id="10", status="running"))
    issues = [{"number": 10, "title": "Bug A", "body": "desc A"}]
    monkeypatch.setenv("ASOC_SWEEP_REPOS", "octo/repo")
    monkeypatch.setattr(main, "list_labeled_issues", lambda repo, label: issues)

    result = main.run_sweep()
    assert result["enqueued"] == 0
    assert result["skipped"] == 1
    assert store.count() == 1


def test_run_sweep_mixed_new_and_existing(store, monkeypatch):
    store.insert_task("existing", make_task(repository="octo/repo", issue_id="10", status="queued"))
    issues = [
        {"number": 10, "title": "Bug A", "body": "desc A"},
        {"number": 30, "title": "Bug C", "body": "desc C"},
    ]
    monkeypatch.setenv("ASOC_SWEEP_REPOS", "octo/repo")
    monkeypatch.setattr(main, "list_labeled_issues", lambda repo, label: issues)

    result = main.run_sweep()
    assert result["enqueued"] == 1
    assert result["skipped"] == 1
    assert store.count() == 2


def test_run_sweep_no_repos(monkeypatch):
    monkeypatch.delenv("ASOC_SWEEP_REPOS", raising=False)
    result = main.run_sweep()
    assert result["enqueued"] == 0
    assert result["skipped"] == 0
    assert result["errors"] == 0


def test_run_sweep_uses_custom_label(store, monkeypatch):
    monkeypatch.setenv("ASOC_SWEEP_REPOS", "octo/repo")
    monkeypatch.setenv("ASOC_SWEEP_LABEL", "auto-fix")
    captured_labels = []

    def mock_list(repo, label):
        captured_labels.append(label)
        return []

    monkeypatch.setattr(main, "list_labeled_issues", mock_list)
    main.run_sweep()
    assert captured_labels == ["auto-fix"]


def test_resolve_acu_per_minute_unset(monkeypatch):
    monkeypatch.delenv("ASOC_ACU_PER_MINUTE", raising=False)
    assert main.resolve_acu_per_minute() is None


def test_resolve_acu_per_minute_valid(monkeypatch):
    monkeypatch.setenv("ASOC_ACU_PER_MINUTE", "0.5")
    assert main.resolve_acu_per_minute() == 0.5


def test_resolve_acu_per_minute_invalid(monkeypatch):
    monkeypatch.setenv("ASOC_ACU_PER_MINUTE", "bad")
    assert main.resolve_acu_per_minute() is None


def test_resolve_acu_per_minute_zero(monkeypatch):
    monkeypatch.setenv("ASOC_ACU_PER_MINUTE", "0")
    assert main.resolve_acu_per_minute() is None


def test_estimate_acu_from_duration(monkeypatch):
    monkeypatch.setenv("ASOC_ACU_PER_MINUTE", "0.5")
    task = {"created_at": 1000.0, "updated_at": 1600.0}
    result = main.estimate_acu_from_duration(task)
    assert result == 5.0


def test_estimate_acu_from_duration_no_rate(monkeypatch):
    monkeypatch.delenv("ASOC_ACU_PER_MINUTE", raising=False)
    task = {"created_at": 1000.0, "updated_at": 1600.0}
    assert main.estimate_acu_from_duration(task) is None


def test_estimate_acu_from_duration_missing_timestamps(monkeypatch):
    monkeypatch.setenv("ASOC_ACU_PER_MINUTE", "0.5")
    assert main.estimate_acu_from_duration({"created_at": None, "updated_at": None}) is None
    assert main.estimate_acu_from_duration({}) is None


def test_backfill_acu_estimates(store, monkeypatch):
    monkeypatch.setenv("ASOC_ACU_PER_MINUTE", "1.0")
    store.insert_task("t1", make_task(status="completed", created_at=1000.0, updated_at=1120.0, acu_used=None))
    store.insert_task("t2", make_task(status="failed", created_at=2000.0, updated_at=2300.0, acu_used=None))
    store.insert_task("t3", make_task(status="completed", created_at=3000.0, updated_at=3060.0, acu_used=5.0))

    count = main.backfill_acu_estimates()
    assert count == 2

    t1 = store.get_task("t1")
    assert t1["acu_used"] == 2.0
    assert t1["acu_estimated"] == 1

    t2 = store.get_task("t2")
    assert t2["acu_used"] == 5.0
    assert t2["acu_estimated"] == 1

    t3 = store.get_task("t3")
    assert t3["acu_used"] == 5.0
    assert t3["acu_estimated"] == 0


def test_backfill_acu_estimates_no_rate(store, monkeypatch):
    monkeypatch.delenv("ASOC_ACU_PER_MINUTE", raising=False)
    store.insert_task("t1", make_task(status="completed", created_at=1000.0, updated_at=1120.0, acu_used=None))
    count = main.backfill_acu_estimates()
    assert count == 0
    assert store.get_task("t1")["acu_used"] is None


def test_acu_estimated_column_roundtrip(store):
    store.insert_task("t1", make_task(acu_used=3.5, acu_estimated=1))
    task = store.get_task("t1")
    assert task["acu_estimated"] == 1
    assert task["acu_used"] == 3.5
