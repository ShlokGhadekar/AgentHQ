from fastapi.testclient import TestClient


def test_register(client: TestClient) -> None:
    response = client.post("/users", json={"username": "alice", "password": "secret123"})
    assert response.status_code == 201
    assert response.json() == {"id": 1, "username": "alice"}


def test_register_duplicate_username(client: TestClient) -> None:
    client.post("/users", json={"username": "alice", "password": "secret123"})
    response = client.post("/users", json={"username": "alice", "password": "other"})
    assert response.status_code == 409


def test_register_short_username(client: TestClient) -> None:
    response = client.post("/users", json={"username": "al", "password": "secret123"})
    assert response.status_code == 422


def test_login_returns_token(client: TestClient) -> None:
    client.post("/users", json={"username": "alice", "password": "secret123"})
    response = client.post("/login", json={"username": "alice", "password": "secret123"})
    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert response.json()["access_token"]


def test_login_wrong_password(client: TestClient) -> None:
    client.post("/users", json={"username": "alice", "password": "secret123"})
    response = client.post("/login", json={"username": "alice", "password": "wrong"})
    assert response.status_code == 401


def test_login_unknown_user(client: TestClient) -> None:
    response = client.post("/login", json={"username": "nobody", "password": "x"})
    assert response.status_code == 401


def test_protected_endpoint_needs_token(client: TestClient) -> None:
    assert client.get("/notes").status_code == 401
    assert client.get("/notes", headers={"Authorization": "Bearer not-a-token"}).status_code == 401
