"""Test for the health-check endpoint (Task 0.3).

Uses FastAPI's TestClient to assert GET /api/health returns HTTP 200 and the
exact JSON body {"status": "ok"}.
"""

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_returns_200_and_ok_body() -> None:
    client = TestClient(create_app())
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
