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


def test_title_is_stripped() -> None:
    client = fresh_client()
    headers = login(client)
    created = client.post("/notes", json={"title": "  Groceries \n", "body": "milk"}, headers=headers)
    assert created.status_code == 201
    assert created.json()["title"] == "Groceries"
    assert client.get(f"/notes/{created.json()['id']}", headers=headers).json()["title"] == "Groceries"


def test_blank_title_is_rejected() -> None:
    client = fresh_client()
    headers = login(client)
    assert client.post("/notes", json={"title": "   "}, headers=headers).status_code == 422
