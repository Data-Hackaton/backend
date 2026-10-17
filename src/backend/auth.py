"""Ключи доступа для ботов и жюри: мастер-ключ ADMIN_KEY из env + токены, выпущенные в UI.

Ключ передаётся в X-API-Key (как требует ТЗ) или в Authorization: Bearer. В БД хранится только sha256 токена.
Методы интерфейса открыты (решение команды на время хакатона), ключ нужен только API для ботов и жюри.
"""

import hashlib
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import config
from backend.db import ApiToken, get_db, now

TOKEN_PREFIX = "fva_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def require_key(
    db: Annotated[Session, Depends(get_db)],
    x_api_key: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """Имя того, кто пришёл: admin или имя токена; иначе 401."""
    key = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None)
    if not key:
        raise HTTPException(status_code=401, detail="missing X-API-Key")
    if config.ADMIN_KEY and secrets.compare_digest(key, config.ADMIN_KEY):
        return "admin"
    token = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(key), ApiToken.revoked_at.is_(None)))
    if token is None:
        raise HTTPException(status_code=401, detail="invalid X-API-Key")
    token.last_used_at = now()
    db.commit()
    return token.name
