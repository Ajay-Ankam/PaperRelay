"""FastAPI health endpoint – no live API calls, no Groq."""

from fastapi.testclient import TestClient

from paperrelay.api.main import app

client = TestClient(app)


def test_health_returns_ok():
    resp = client.get("/health")
    assert resp.status_code == 200


def test_health_body():
    resp = client.get("/health")
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_health_unknown_path_is_404():
    resp = client.get("/nonexistent")
    assert resp.status_code == 404
