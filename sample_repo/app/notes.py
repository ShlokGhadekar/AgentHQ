"""CRUD endpoints for the current user's notes."""

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.auth import get_current_user
from app.models import NoteCreate, NoteOut
from app.storage import Note, User, store

router = APIRouter(prefix="/notes", tags=["notes"])


def to_out(note: Note) -> NoteOut:
    return NoteOut(id=note.id, title=note.title, body=note.body, created_at=note.created_at)


def get_own_note(note_id: int, user: User) -> Note:
    note = store.notes.get(note_id)
    # Someone else's note gets the same 404 as a missing one, so ids don't leak.
    if note is None or note.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    return note


@router.get("", response_model=list[NoteOut])
def list_notes(user: User = Depends(get_current_user)) -> list[NoteOut]:
    return [to_out(note) for note in sorted(store.notes_for(user.id), key=lambda n: n.id)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=NoteOut)
def create_note(body: NoteCreate, user: User = Depends(get_current_user)) -> NoteOut:
    return to_out(store.add_note(user.id, body.title, body.body))


@router.get("/{note_id}", response_model=NoteOut)
def read_note(note_id: int, user: User = Depends(get_current_user)) -> NoteOut:
    return to_out(get_own_note(note_id, user))


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_note(note_id: int, user: User = Depends(get_current_user)) -> Response:
    note = get_own_note(note_id, user)
    del store.notes[note.id]
    return Response(status_code=status.HTTP_204_NO_CONTENT)
