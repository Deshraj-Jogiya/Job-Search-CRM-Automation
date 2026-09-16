"""CSRFMiddleware's exemption list (app/csrf.py) has no test coverage
at all, despite gating every POST in the app. Added alongside the
2026-09-16 change adding /api/extension/ (the browser extension's API,
authenticated by an explicit header instead of the CSRF cookie -- see
routers/extension.py's docstring for why). A too-broad exemption prefix
would silently disable CSRF protection for unrelated routes; too narrow
and the extension's real calls would 403."""

from app.csrf import _is_exempt


def test_extension_api_paths_are_exempt():
    assert _is_exempt("/api/extension/match")
    assert _is_exempt("/api/extension/answers")


def test_confirm_links_remain_exempt():
    assert _is_exempt("/confirm/abc123")


def test_health_check_remains_exempt():
    assert _is_exempt("/api/health")


def test_a_similarly_named_but_different_path_is_not_exempt():
    # Guards against an overly loose prefix match swallowing a real,
    # unrelated route that happens to share a substring.
    assert not _is_exempt("/api/extension-settings")
    assert not _is_exempt("/api/extensions")


def test_ordinary_form_routes_stay_protected():
    assert not _is_exempt("/jobs")
    assert not _is_exempt("/jobs/1/approve")
    assert not _is_exempt("/jobs/1/kanban-move")
