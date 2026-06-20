import json

REMEDIATE_BODY = {
    "issue_id": "5",
    "title": "Race condition",
    "description": "Intermittent failures.",
    "repository": "octo/repo",
}


def test_remediate_dedup(client):
    first = client.post("/remediate", json=REMEDIATE_BODY).json()
    assert first["status"] == "accepted"
    second = client.post("/remediate", json=REMEDIATE_BODY).json()
    assert second["status"] == "duplicate"
    assert second["task_id"] == first["task_id"]


def test_webhook_dedup(client):
    payload = {
        "action": "labeled",
        "label": {"name": "trigger-devin"},
        "issue": {"number": 9, "title": "Bug", "body": "broken"},
        "repository": {"full_name": "octo/repo"},
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {"X-GitHub-Event": "issues", "Content-Type": "application/json"}
    first = client.post("/webhooks/github", content=body, headers=headers).json()
    assert first["status"] == "webhook_processed"
    second = client.post("/webhooks/github", content=body, headers=headers).json()
    assert second["status"] == "duplicate"
    assert second["task_id"] == first["task_id"]
