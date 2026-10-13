"""Password hashing and token generation."""

import hashlib
import hmac
import secrets


def new_salt() -> str:
    return secrets.token_hex(16)


def hash_password(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return digest.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_password(password, salt), expected_hash)


def new_token() -> str:
    return secrets.token_urlsafe(32)
