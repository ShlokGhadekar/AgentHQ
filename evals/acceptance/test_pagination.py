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


def make_notes(client: TestClient, headers: dict[str, str], count: int) -> None:
    for i in range(1, count + 1):
        client.post("/notes", json={"title": f"note {i}"}, headers=headers)


def test_default_page_is_twenty() -> None:
    client = fresh_client()
    headers = login(client)
    make_notes(client, headers, 25)
    titles = [n["title"] for n in client.get("/notes", headers=headers).json()]
    assert titles == [f"note {i}" for i in range(1, 21)]


def test_limit_and_offset() -> None:
    client = fresh_client()
    headers = login(client)
    make_notes(client, headers, 25)
    page = client.get("/notes", params={"limit": 5, "offset": 10}, headers=headers).json()
    assert [n["title"] for n in page] == [f"note {i}" for i in range(11, 16)]
    assert client.get("/notes", params={"offset": 30}, headers=headers).json() == []


def test_out_of_range_values_are_rejected() -> None:
    client = fresh_client()
    headers = login(client)
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        assert client.get("/notes", params=params, headers=headers).status_code == 422, params
