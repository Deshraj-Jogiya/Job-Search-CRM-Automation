"""
Shared Jinja2 template renderer. Split out from main.py so routers
(app/routers/*) can render pages without importing main.py itself and
risking a circular import.
"""

import os

from fastapi import Request
from fastapi.templating import Jinja2Templates
from .csrf import get_csrf_token
from .services import auth_service

templates = Jinja2Templates(directory="app/templates")

_STATIC_CSS_PATH = os.path.join("app", "static", "css", "style.css")


def _static_version() -> str:
    """Cache-busting query param for style.css, derived from its own
    mtime -- browsers cache a static asset aggressively with no explicit
    Cache-Control header, so without this, a CSS change can silently keep
    showing a stale stylesheet to an already-open tab/browser cache until
    a hard refresh. Falls back to a fixed value if the file can't be
    stat'd (e.g. an unusual deployment layout) rather than erroring."""
    try:
        return str(int(os.path.getmtime(_STATIC_CSS_PATH)))
    except OSError:
        return "0"


def render(request: Request, template_name: str, context: dict):
    context["csrf_token"] = get_csrf_token(request)
    context.setdefault("static_version", _static_version())
    # Cheap, DB-free check (pure HMAC + expiry) -- _base.html uses this
    # to decide whether to show "Log Out", instead of checking raw
    # cookie presence, which stays true even after the underlying
    # session/account is gone (e.g. an account got deleted, or the
    # session simply expired) and would show a Log Out button that
    # doesn't correspond to a real active session.
    session_token = request.cookies.get(auth_service.SESSION_COOKIE_NAME)
    context.setdefault("is_authenticated", bool(session_token and auth_service.verify_session_token(session_token)))
    return templates.TemplateResponse(request, template_name, context)
