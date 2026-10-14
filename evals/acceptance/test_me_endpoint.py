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


def test_me_reports_the_user_and_note_count() -> None:
    client = fresh_client()
    login(client, "bob")
    headers = login(client, "alice")
    for title in ("one", "two"):
        client.post("/notes", json={"title": title}, headers=headers)
    response = client.get("/me", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"id": 2, "username": "alice", "note_count": 2}


def test_me_needs_a_token() -> None:
    assert fresh_client().get("/me").status_code == 401
