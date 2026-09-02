from __future__ import annotations

import hmac
import os
from typing import Any

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def http_bind_allowed(host: str, token: str | None = None) -> tuple[bool, str]:
    """Fail-closed bind policy: non-loopback hosts require a non-empty bearer token."""
    normalized = str(host or "").strip().lower()
    if not normalized:
        return False, "host is required"
    if normalized in LOOPBACK_HOSTS:
        return True, ""
    if token and str(token).strip():
        return True, ""
    return False, "non-loopback HTTP bind requires HAI_HTTP_TOKEN"


def verify_bearer_token(authorization: str | None, expected_token: str) -> bool:
    if not authorization or not str(authorization).startswith("Bearer "):
        return False
    provided = authorization[7:].strip()
    if not provided or not expected_token:
        return False
    return hmac.compare_digest(provided, expected_token)


def http_token_from_env() -> str | None:
    raw = os.environ.get("HAI_HTTP_TOKEN", "").strip()
    return raw or None


def wrap_with_bearer_token(app: Any, expected_token: str) -> Any:
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    class _BearerTokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
            if not verify_bearer_token(request.headers.get("authorization"), expected_token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    return _BearerTokenMiddleware(app)
