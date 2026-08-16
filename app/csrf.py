"""
Signed double-submit-cookie CSRF protection for form-based POST routes.
No server-side session storage needed.
"""

import os
import hmac
import hashlib
import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from fastapi import HTTPException

CSRF_COOKIE_NAME = "csrf_token"
CSRF_FORM_FIELD = "csrf_token"

_secret = os.getenv("SECRET_KEY")
if not _secret:
    raise RuntimeError(
        "SECRET_KEY is not set. Generate one with:\n"
        "  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
        "and add it to .env."
    )


def _sign(raw_value: str) -> str:
    sig = hmac.new(_secret.encode(), raw_value.encode(), hashlib.sha256).hexdigest()
    return f"{raw_value}.{sig}"


def _verify(token: str) -> bool:
    if not token or "." not in token:
        return False
    raw_value, _, sig = token.rpartition(".")
    expected = hmac.new(_secret.encode(), raw_value.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _generate_token() -> str:
    return _sign(secrets.token_urlsafe(24))


def get_csrf_token(request: Request) -> str:
    return getattr(request.state, "csrf_token", "")


EXEMPT_PATHS = {"/api/health"}


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming_token = request.cookies.get(CSRF_COOKIE_NAME)

        if request.method == "POST" and request.url.path not in EXEMPT_PATHS:
            content_type = request.headers.get("content-type", "")
            form_token = None
            if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
                form = await request.form()
                form_token = form.get(CSRF_FORM_FIELD)
                request._form = form

            if not incoming_token or not form_token or not _verify(incoming_token) or not hmac.compare_digest(incoming_token, form_token):
                raise HTTPException(status_code=403, detail="CSRF validation failed. Please refresh and try again.")

        if incoming_token and _verify(incoming_token):
            request.state.csrf_token = incoming_token
            rotate = False
        else:
            request.state.csrf_token = _generate_token()
            rotate = True

        response: Response = await call_next(request)

        if rotate:
            response.set_cookie(
                CSRF_COOKIE_NAME,
                request.state.csrf_token,
                httponly=True,
                samesite="strict",
                secure=os.getenv("ENV", "development") == "production",
            )

        return response
