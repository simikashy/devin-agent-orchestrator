REMEDIATE_BODY = {
    "issue_id": "1",
    "title": "Broken login",
    "description": "Users cannot sign in.",
    "repository": "octo/repo",
}


def test_remediate_open_when_token_unset(client):
    response = client.post("/remediate", json=REMEDIATE_BODY)
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


def test_remediate_requires_token_when_set(client, monkeypatch):
    monkeypatch.setenv("ASOC_API_TOKEN", "secret")
    assert client.post("/remediate", json=REMEDIATE_BODY).status_code == 401
    assert (
        client.post("/remediate", json=REMEDIATE_BODY, headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    accepted = client.post("/remediate", json=REMEDIATE_BODY, headers={"Authorization": "Bearer secret"})
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"


def test_metrics_open_when_token_unset(client):
    assert client.get("/metrics").status_code == 200


def test_metrics_requires_token_when_set(client, monkeypatch):
    monkeypatch.setenv("ASOC_DASHBOARD_TOKEN", "dash")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer dash"}).status_code == 200
