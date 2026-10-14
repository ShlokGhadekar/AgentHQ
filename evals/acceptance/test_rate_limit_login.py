"""Hidden acceptance test. Copied into the clone after the run and run on its own; agents never see it."""

from fastapi.testclient import TestClient

from app.main import app
from app.storage import reset_store

PASSWORD = "correct-horse-battery"


def fresh_client() -> TestClient:
    reset_store()
    return TestClient(app)


def login(client: TestClient, username: str = "alice") -> dict[str, str]:
    client.post("/users", json={"username": username, "password": PASSWORD})
    token = client.post("/login", json={"username": username, "password": PASSWORD}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_sixth_attempt_in_a_minute_is_limited() -> None:
    client = fresh_client()
    client.post("/users", json={"username": "alice", "password": PASSWORD})
    for _ in range(5):
        assert client.post("/login", json={"username": "alice", "password": "wrong-password"}).status_code == 401
    # Correct credentials don't help once the limit is reached: every attempt counts.
    limited = client.post("/login", json={"username": "alice", "password": PASSWORD})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0
