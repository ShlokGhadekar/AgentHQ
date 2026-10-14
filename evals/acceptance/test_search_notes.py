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


def test_search_matches_title_or_body_ignoring_case() -> None:
    client = fresh_client()
    headers = login(client)
    client.post("/notes", json={"title": "Groceries", "body": "milk, eggs"}, headers=headers)
    client.post("/notes", json={"title": "Ideas", "body": "Buy MILK frother"}, headers=headers)
    client.post("/notes", json={"title": "Travel", "body": "passport"}, headers=headers)

    def titles(query: str) -> list[str]:
        return [n["title"] for n in client.get("/notes", params={"q": query}, headers=headers).json()]

    assert titles("milk") == ["Groceries", "Ideas"]
    assert titles("IDEA") == ["Ideas"]
    assert titles("nothing-like-this") == []
    assert len(client.get("/notes", headers=headers).json()) == 3


def test_search_only_sees_your_own_notes() -> None:
    client = fresh_client()
    bob = login(client, "bob")
    client.post("/notes", json={"title": "milk run"}, headers=bob)
    alice = login(client, "alice")
    assert client.get("/notes", params={"q": "milk"}, headers=alice).json() == []
