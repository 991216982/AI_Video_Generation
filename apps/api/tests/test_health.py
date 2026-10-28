from fastapi.testclient import TestClient

from book_video.main import app


def test_health() -> None:
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
