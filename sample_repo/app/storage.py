"""In-memory storage for users, tokens and notes."""

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class User:
    id: int
    username: str
    password_hash: str
    salt: str


@dataclass
class Note:
    id: int
    owner_id: int
    title: str
    body: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Session:
    user_id: int
    expires_at: datetime


class Store:
    def __init__(self) -> None:
        self.users: dict[str, User] = {}
        self.sessions: dict[str, Session] = {}
        self.notes: dict[int, Note] = {}
        self._next_user_id = 1
        self._next_note_id = 1

    def add_user(self, username: str, password_hash: str, salt: str) -> User:
        user = User(self._next_user_id, username, password_hash, salt)
        self.users[username] = user
        self._next_user_id += 1
        return user

    def get_user_by_id(self, user_id: int) -> User | None:
        return next((u for u in self.users.values() if u.id == user_id), None)

    def add_note(self, owner_id: int, title: str, body: str) -> Note:
        note = Note(self._next_note_id, owner_id, title, body)
        self.notes[note.id] = note
        self._next_note_id += 1
        return note

    def notes_for(self, owner_id: int) -> list[Note]:
        return [n for n in self.notes.values() if n.owner_id == owner_id]


store = Store()


def reset_store() -> None:
    """Start from empty storage (used by tests)."""
    store.__init__()
