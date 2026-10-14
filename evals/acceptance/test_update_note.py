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


def test_update_replaces_title_and_body() -> None:
    client = fresh_client()
    headers = login(client)
    original = client.post("/notes", json={"title": "Draft", "body": "v1"}, headers=headers).json()
    response = client.put(f"/notes/{original['id']}", json={"title": "Final", "body": "v2"}, headers=headers)
    assert response.status_code == 200
    updated = response.json()
    assert (updated["id"], updated["title"], updated["body"]) == (original["id"], "Final", "v2")
    assert updated["created_at"] == original["created_at"]
    assert client.get(f"/notes/{original['id']}", headers=headers).json()["title"] == "Final"


def test_update_validates_like_create() -> None:
    client = fresh_client()
    headers = login(client)
    note_id = client.post("/notes", json={"title": "Draft"}, headers=headers).json()["id"]
    assert client.put(f"/notes/{note_id}", json={"title": "", "body": "x"}, headers=headers).status_code == 422


def test_cannot_update_missing_or_foreign_notes() -> None:
    client = fresh_client()
    alice = login(client, "alice")
    note_id = client.post("/notes", json={"title": "Mine"}, headers=alice).json()["id"]
    bob = login(client, "bob")
    assert client.put(f"/notes/{note_id}", json={"title": "Stolen"}, headers=bob).status_code == 404
    assert client.put("/notes/999", json={"title": "x"}, headers=alice).status_code == 404
    assert client.get(f"/notes/{note_id}", headers=alice).json()["title"] == "Mine"
