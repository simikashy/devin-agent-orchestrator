def test_dashboard_serves_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "ASOC Orchestrator Dashboard" in response.text
    assert "Export CSV" in response.text
    assert 'id="leaderboard-body"' in response.text
    assert "/static/chart.umd.min.js" in response.text


def test_healthz_ok(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_static_chart_served(client):
    response = client.get("/static/chart.umd.min.js")
    assert response.status_code == 200
    assert "Chart" in response.text
