"""Request and response bodies."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.config import settings


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1)


class UserOut(BaseModel):
    id: int
    username: str


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class NoteCreate(BaseModel):
    title: str = Field(min_length=1, max_length=settings.max_title_length)
    body: str = Field(default="", max_length=settings.max_body_length)


class NoteOut(BaseModel):
    id: int
    title: str
    body: str
    created_at: datetime
