"""Shared access key for the API and a short-lived signed browser session."""

import asyncio
import re
import secrets
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie, APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.datastructures import MutableHeaders

from backend import config, deadlines

COOKIE_NAME = "fva_session"
SESSION_SECONDS = 12 * 60 * 60
API_KEY = APIKeyHeader(name="X-API-Key", scheme_name="X-API-Key", auto_error=False,
                       description="Общий ключ доступа ADMIN_KEY")
BEARER = HTTPBearer(scheme_name="Bearer", auto_error=False, description="Общий ключ доступа")
COOKIE = APIKeyCookie(name=COOKIE_NAME, scheme_name="BrowserSession", auto_error=False)


def valid_key(key: str | None) -> bool:
    return bool(key and config.ADMIN_KEY and secrets.compare_digest(key.encode(), config.ADMIN_KEY.encode()))


def signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.ADMIN_KEY, salt="forte-browser-session-v1")


def new_session() -> str:
    return signer().dumps({"access": "admin", "nonce": secrets.token_urlsafe(16)})


def valid_session(value: str | None) -> bool:
    if not value or not config.ADMIN_KEY:
        return False
    try:
        data = signer().loads(value, max_age=SESSION_SECONDS)
        return isinstance(data, dict) and data.get("access") == "admin"
    except BadSignature:
        return False


def session_expiry(request: Request) -> int | None:
    if request.headers.get("x-api-key") is not None or request.headers.get("authorization") is not None:
        return None
    if not valid_session(request.cookies.get(COOKIE_NAME)):
        return None
    _, issued = signer().loads(request.cookies[COOKIE_NAME], max_age=SESSION_SECONDS, return_timestamp=True)
    return int(issued.timestamp()) + SESSION_SECONDS


def authentication(request: Request) -> str | None:
    """Explicit bad credentials never fall back to an existing browser cookie."""
    key = request.headers.get("x-api-key")
    authorization = request.headers.get("authorization")
    if key is not None:
        return "key" if valid_key(key) else None
    if authorization is not None:
        scheme, _, value = authorization.partition(" ")
        return "key" if scheme.lower() == "bearer" and valid_key(value) else None
    return "cookie" if valid_session(request.cookies.get(COOKIE_NAME)) else None


def require_same_origin(request: Request) -> None:
    """Cookie writes need a browser Origin matching the externally visible host/scheme."""
    origin = request.headers.get("origin")
    expected_scheme = "https" if config.SESSION_COOKIE_SECURE else request.url.scheme
    try:
        parsed = urlsplit(origin or "")
        same = (parsed.scheme == expected_scheme and parsed.netloc.lower() == request.headers.get("host", "").lower()
                and not parsed.path and not parsed.query and not parsed.fragment)
    except ValueError:
        same = False
    if not same or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Запрос должен исходить с этого сайта")


def require_key(
    request: Request,
    x_api_key: Annotated[str | None, Security(API_KEY)] = None,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Security(BEARER)] = None,
    session: Annotated[str | None, Security(COOKIE)] = None,
) -> str:
    if not authentication(request):
        raise HTTPException(status_code=401, detail="Требуется токен доступа")
    return "admin"


def require_jury_key(x_api_key: Annotated[str | None, Security(API_KEY)] = None) -> str:
    """The two jury endpoints require the actual X-API-Key header."""
    if not valid_key(x_api_key):
        raise HTTPException(status_code=401, detail="Требуется действительный X-API-Key")
    return "admin"


class AccessMiddleware:
    """Protect API/media before reading upload bodies; keep streaming responses streaming."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/")
        protected = path == "/api" or path.startswith("/api/") or path in {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
        jury = path == "/api/v1/analyze" or re.fullmatch(r"/api/v1/jobs/[^/]+", path) is not None
        response_started = False

        async def no_cache(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            if protected and message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["Vary"] = ", ".join(filter(None, [headers.get("Vary"), "Cookie, Authorization, X-API-Key"]))
            await send(message)

        if protected and path not in {"/api/v1/health", "/api/v1/auth/session"}:
            request = Request(scope)
            mode = authentication(request)
            try:
                if jury:
                    require_jury_key(request.headers.get("x-api-key"))
                if not mode:
                    raise HTTPException(status_code=401, detail="Требуется токен доступа")
                if mode == "cookie" and request.method not in {"GET", "HEAD", "OPTIONS"}:
                    require_same_origin(request)
            except HTTPException as exc:
                return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, no_cache)
        if path == "/api/v1/analyze" and scope["method"] == "POST":
            scope.setdefault("state", {})["received_at"] = datetime.now(UTC)
            try:
                with deadlines.budget(config.JOB_TIMEOUT_SEC):
                    async with asyncio.timeout(config.JOB_TIMEOUT_SEC):
                        await self.app(scope, receive, no_cache)
            except TimeoutError:
                if response_started:
                    raise
                await JSONResponse({"detail": "video processing deadline exceeded"}, status_code=504)(
                    scope, receive, no_cache)
            return
        await self.app(scope, receive, no_cache)
