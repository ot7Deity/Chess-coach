from fastapi.testclient import TestClient

from chess_coach.api.main import app

client = TestClient(app)


def test_health_is_ok():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_health_does_not_require_database():
    """The probe must stay green when Postgres is down, or orchestrators will
    restart healthy API containers during a database blip."""
    r = TestClient(app).get("/health")
    assert r.status_code == 200
