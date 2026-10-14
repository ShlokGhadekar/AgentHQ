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


def test_logout_invalidates_only_that_token() -> None:
    client = fresh_client()
    first = login(client)
    token = client.post("/login", json={"username": "alice", "password": PASSWORD}).json()["access_token"]
    second = {"Authorization": f"Bearer {token}"}

    response = client.post("/logout", headers=first)
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/notes", headers=first).status_code == 401
    assert client.get("/notes", headers=second).status_code == 200


def test_logout_needs_a_token() -> None:
    assert fresh_client().post("/logout").status_code == 401
