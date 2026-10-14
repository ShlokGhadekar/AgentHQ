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


def test_short_passwords_are_rejected() -> None:
    client = fresh_client()
    assert client.post("/users", json={"username": "alice", "password": "1234567"}).status_code == 422


def test_eight_characters_are_enough() -> None:
    client = fresh_client()
    assert client.post("/users", json={"username": "alice", "password": "12345678"}).status_code == 201
