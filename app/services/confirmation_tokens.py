"""
Signed, time-limited bearer tokens for one-click email approve/reject
links. Deliberately separate from app/csrf.py's cookie-based double-
submit flow: an email is opened on whatever device/browser the user
has in hand at the time, which won't carry the dashboard's session
cookie. This is the standard "magic link" pattern instead -- the
token itself, not a cookie, proves the click came from the email sent
for this specific application.
"""

import os
import hmac
import hashlib
from datetime import timedelta

from ..database import utcnow

_secret = os.getenv("SECRET_KEY")
if not _secret:
    raise RuntimeError(
        "SECRET_KEY is not set. Generate one with:\n"
        "  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
        "and add it to .env."
    )

# Generous window -- a Needs Review item might genuinely sit for a day or
# more waiting on the user, and the link shouldn't go stale before they
# get to it. The standard/fast-track confirmation windows are much
# shorter than this anyway, so this is just an outer safety bound.
TOKEN_VALID_DAYS = 30


def generate_token(application_id: int) -> str:
    expires_at = int((utcnow() + timedelta(days=TOKEN_VALID_DAYS)).timestamp())
    raw = f"{application_id}:{expires_at}"
    sig = hmac.new(_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


def verify_token(token: str, application_id: int) -> bool:
    if not token or "." not in token:
        return False
    try:
        raw, sig = token.rsplit(".", 1)
        app_id_str, expires_at_str = raw.split(":")
    except ValueError:
        return False

    if int(app_id_str) != application_id:
        return False
    # .timestamp() on a naive datetime is interpreted as local time, not
    # UTC -- pre-existing on both sides of this comparison (generate_token
    # has the same behavior), so it's internally consistent, just off from
    # a true Unix timestamp by the local UTC offset. Harmless in practice
    # given TOKEN_VALID_DAYS' generous 30-day margin, but a real thing to
    # fix properly (e.g. utcnow().replace(tzinfo=timezone.utc).timestamp())
    # if this code is touched for a reason that makes the offset matter.
    if int(expires_at_str) < utcnow().timestamp():
        return False

    expected = hmac.new(_secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)
