from fastapi.testclient import TestClient

from tests.conftest import register_and_login


def test_create_and_list_notes(client: TestClient, auth_headers: dict[str, str]) -> None:
    created = client.post("/notes", json={"title": "Groceries", "body": "milk"}, headers=auth_headers)
    assert created.status_code == 201
    assert created.json()["title"] == "Groceries"

    listed = client.get("/notes", headers=auth_headers)
    assert [n["title"] for n in listed.json()] == ["Groceries"]


def test_read_note(client: TestClient, auth_headers: dict[str, str]) -> None:
    note_id = client.post("/notes", json={"title": "Ideas"}, headers=auth_headers).json()["id"]
    response = client.get(f"/notes/{note_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["body"] == ""


def test_delete_note(client: TestClient, auth_headers: dict[str, str]) -> None:
    note_id = client.post("/notes", json={"title": "Temp"}, headers=auth_headers).json()["id"]
    assert client.delete(f"/notes/{note_id}", headers=auth_headers).status_code == 204
    assert client.get(f"/notes/{note_id}", headers=auth_headers).status_code == 404


def test_cannot_see_other_users_notes(client: TestClient, auth_headers: dict[str, str]) -> None:
    note_id = client.post("/notes", json={"title": "Private"}, headers=auth_headers).json()["id"]
    bob = register_and_login(client, "bob")
    assert client.get(f"/notes/{note_id}", headers=bob).status_code == 404
    assert client.get("/notes", headers=bob).json() == []


def test_empty_title_is_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post("/notes", json={"title": ""}, headers=auth_headers)
    assert response.status_code == 422


def test_missing_note_is_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    assert client.get("/notes/999", headers=auth_headers).status_code == 404
