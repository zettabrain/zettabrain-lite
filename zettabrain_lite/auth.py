"""ZettaBrain Lite — JWT authentication helpers."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import HTTPException, Request
from sqlmodel import select

from .config import get_setting, set_setting
from .database import User, get_session

TOKEN_EXPIRY_DAYS = 7
ALGORITHM = "HS256"


def _get_or_create_secret() -> str:
    secret = get_setting("jwt_secret")
    if not secret:
        secret = secrets.token_urlsafe(32)
        set_setting("jwt_secret", secret)
    return secret


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRY_DAYS),
    }
    return jwt.encode(payload, _get_or_create_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[str]:
    try:
        payload = jwt.decode(token, _get_or_create_secret(), algorithms=[ALGORITHM])
        return payload.get("sub")
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def get_current_user(request: Request) -> Optional[str]:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return decode_token(auth_header[7:])
    return None


def require_auth(request: Request) -> str:
    username = get_current_user(request)
    if not username:
        raise HTTPException(status_code=401, detail="Please log in to continue.")
    return username


def register_user(username: str, password: str) -> User:
    if len(username) < 3 or len(username) > 30:
        raise HTTPException(status_code=400, detail="Username must be 3-30 characters.")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    with get_session() as session:
        existing = session.exec(select(User).where(User.username == username)).first()
        if existing:
            raise HTTPException(status_code=409, detail="That username is taken. Choose a different one.")
        user = User(username=username, password_hash=hash_password(password))
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def authenticate_user(username: str, password: str) -> User:
    with get_session() as session:
        user = session.exec(select(User).where(User.username == username)).first()
        if not user or not verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="Incorrect username or password.")
        return user
