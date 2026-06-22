def make_task(**overrides):
    task = {
        "issue_id": "1",
        "title": "Example issue",
        "repository": "octo/repo",
        "branch": "main",
        "status": "queued",
        "session_id": None,
        "pr_url": None,
        "failure_category": None,
        "failure_reason": None,
        "created_at": 100.0,
        "updated_at": 100.0,
        "error": None,
        "session_url": None,
        "acu_used": None,
        "acu_estimated": 0,
    }
    task.update(overrides)
    return task
