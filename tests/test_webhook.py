import hashlib
import hmac
import json

WEBHOOK_PAYLOAD = {
    "action": "labeled",
    "label": {"name": "trigger-devin"},
    "issue": {"number": 42, "title": "Bug", "body": "It is broken."},
    "repository": {"full_name": "octo/repo"},
}


def signature(secret, body):
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def post_webhook(client, body, headers):
    base = {"X-GitHub-Event": "issues", "Content-Type": "application/json"}
    base.update(headers)
    return client.post("/webhooks/github", content=body, headers=base)


def test_webhook_valid_signature(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "shh")
    body = json.dumps(WEBHOOK_PAYLOAD).encode("utf-8")
    response = post_webhook(client, body, {"X-Hub-Signature-256": signature("shh", body)})
    assert response.status_code == 200
    assert response.json()["status"] == "webhook_processed"


def test_webhook_invalid_signature(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "shh")
    body = json.dumps(WEBHOOK_PAYLOAD).encode("utf-8")
    response = post_webhook(client, body, {"X-Hub-Signature-256": "sha256=deadbeef"})
    assert response.status_code == 401


def test_webhook_absent_signature_when_secret_set(client, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "shh")
    body = json.dumps(WEBHOOK_PAYLOAD).encode("utf-8")
    response = post_webhook(client, body, {})
    assert response.status_code == 401


def test_webhook_open_when_secret_absent(client):
    body = json.dumps(WEBHOOK_PAYLOAD).encode("utf-8")
    response = post_webhook(client, body, {})
    assert response.status_code == 200
    assert response.json()["status"] == "webhook_processed"


def test_webhook_ignores_non_trigger_label(client):
    payload = dict(WEBHOOK_PAYLOAD)
    payload["label"] = {"name": "documentation"}
    body = json.dumps(payload).encode("utf-8")
    response = post_webhook(client, body, {})
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
