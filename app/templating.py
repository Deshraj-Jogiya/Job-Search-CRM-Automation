"""
Shared Jinja2 template renderer. Split out from main.py so routers
(app/routers/*) can render pages without importing main.py itself and
risking a circular import.
"""

from fastapi import Request
from fastapi.templating import Jinja2Templates
from .csrf import get_csrf_token

templates = Jinja2Templates(directory="app/templates")


def render(request: Request, template_name: str, context: dict):
    context["csrf_token"] = get_csrf_token(request)
    return templates.TemplateResponse(request, template_name, context)
