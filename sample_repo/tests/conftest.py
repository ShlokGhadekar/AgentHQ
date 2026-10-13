import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage import reset_store


@pytest.fixture
def client() -> TestClient:
    reset_store()
    return TestClient(app)


def register_and_login(client: TestClient, username: str = "alice", password: str = "secret123") -> dict[str, str]:
    client.post("/users", json={"username": username, "password": password})
    token = client.post("/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    return register_and_login(client)
