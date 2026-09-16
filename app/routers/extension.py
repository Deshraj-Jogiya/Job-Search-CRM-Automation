"""API for the companion browser extension (see extension/README.md) --
the real fix for the employer-wrapped/bot-blocked ATS case (Samsara:
5/5 real Playwright attempts, 0 fields filled, likely datacenter-IP bot
blocking). Running from the user's own real browser on their own real
residential IP is what actually solves that; see extension_service.py
for the field-matching logic this reuses unchanged from Playwright
autofill.

Auth is intentionally NOT the normal admin_session cookie + CSRF-cookie
flow every other route uses -- the extension calls this from its
background service worker (a chrome-extension:// origin, not a career-
pilot page), so it never receives the CSRF cookie via a real page load,
and forwarding a cookie automatically across that origin is blocked by
the cookie's own SameSite=Strict flag anyway (correctly -- that flag is
what stops a THIRD-PARTY WEBSITE from riding the user's session). The
extension instead reads the existing admin_session cookie value itself
via the privileged chrome.cookies API (bypasses SameSite/HttpOnly by
design -- an extension is not a website) and sends it explicitly as the
X-Career-Pilot-Session header below. That is verified with the exact
same auth_service.verify_session_token() every normal page already
uses -- same session, same expiry, nothing new to revoke or manage.

This also means CSRF's double-submit check does not apply and must not
be required here: a malicious third-party page cannot forge this
header's value (it has no way to read the httponly admin_session
cookie), and this app runs no CORS middleware, so a malicious page's
own script could not read this endpoint's response even if it guessed
right. See csrf.py's EXEMPT_PATH_PREFIXES, which this path is added to.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import auth_service, extension_service

router = APIRouter(prefix="/api/extension", tags=["extension"])


def require_extension_session(request: Request) -> int:
    token = request.headers.get("X-Career-Pilot-Session")
    account_id = auth_service.verify_session_token(token) if token else None
    if account_id is None:
        raise HTTPException(
            status_code=401,
            detail="Not logged in -- log into Career Pilot in this browser, then reload the extension.",
        )
    return account_id


class MatchRequest(BaseModel):
    url: str


class FieldsRequest(BaseModel):
    application_id: int
    fields: list[dict]


@router.post("/match")
def match_current_page(
    body: MatchRequest, db: Session = Depends(get_db), _account_id: int = Depends(require_extension_session)
):
    application = extension_service.find_fillable_application(db, body.url)
    if not application:
        return {"matched": False}
    return {"matched": True, **extension_service.application_match_summary(db, application)}


@router.post("/answers")
def field_answers(
    body: FieldsRequest, db: Session = Depends(get_db), _account_id: int = Depends(require_extension_session)
):
    return extension_service.resolve_field_answers(db, body.application_id, body.fields)
