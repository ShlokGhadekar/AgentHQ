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


def test_delete_account_removes_user_notes_and_tokens() -> None:
    client = fresh_client()
    bob = login(client, "bob")
    client.post("/notes", json={"title": "Bob's"}, headers=bob)
    alice = login(client, "alice")
    client.post("/notes", json={"title": "Alice's"}, headers=alice)

    assert client.delete("/users/me", headers=alice).status_code == 204
    assert client.get("/notes", headers=alice).status_code == 401
    assert client.post("/login", json={"username": "alice", "password": PASSWORD}).status_code == 401
    assert [n["title"] for n in client.get("/notes", headers=bob).json()] == ["Bob's"]


def test_username_can_be_registered_again() -> None:
    client = fresh_client()
    alice = login(client, "alice")
    client.post("/notes", json={"title": "old"}, headers=alice)
    client.delete("/users/me", headers=alice)
    assert client.post("/users", json={"username": "alice", "password": PASSWORD}).status_code == 201
    fresh = login(client, "alice")
    assert client.get("/notes", headers=fresh).json() == []


def test_delete_account_needs_a_token() -> None:
    assert fresh_client().delete("/users/me").status_code == 401
