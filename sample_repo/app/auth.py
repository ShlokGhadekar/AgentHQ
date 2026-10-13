"""Registration, login and the current-user dependency."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.models import LoginRequest, TokenOut, UserCreate, UserOut
from app.security import hash_password, new_salt, new_token, verify_password
from app.storage import Session, User, store

router = APIRouter(tags=["auth"])
bearer = HTTPBearer(auto_error=False)


@router.post("/users", status_code=status.HTTP_201_CREATED, response_model=UserOut)
def register(body: UserCreate) -> UserOut:
    if body.username in store.users:
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already taken")
    salt = new_salt()
    user = store.add_user(body.username, hash_password(body.password, salt), salt)
    return UserOut(id=user.id, username=user.username)


@router.post("/login", response_model=TokenOut)
def login(body: LoginRequest) -> TokenOut:
    user = store.users.get(body.username)
    if user is None or not verify_password(body.password, user.salt, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    token = new_token()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.token_ttl_minutes)
    store.sessions[token] = Session(user_id=user.id, expires_at=expires_at)
    return TokenOut(access_token=token)


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    session = store.sessions.get(credentials.credentials)
    if session is None or session.expires_at < datetime.now(UTC):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    user = store.get_user_by_id(session.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown user")
    return user
