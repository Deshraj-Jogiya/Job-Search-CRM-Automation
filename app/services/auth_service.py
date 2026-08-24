"""
Real login/signup/forgot-password for the one operator account this
deployment has (see AdminAccount's docstring in models.py for why this
is single-account, not multi-tenant).

Password hashing uses bcrypt directly (a real, standard KDF -- never
store or compare a plaintext password). Session and password-reset
tokens are signed the same way app/services/confirmation_tokens.py
already signs magic-link tokens (HMAC-SHA256 with SECRET_KEY), reusing
a proven pattern rather than adding a new dependency (e.g. itsdangerous)
for something this codebase already knows how to do safely.
"""

import hmac
import hashlib
import os
import secrets
from datetime import timedelta

import bcrypt

from ..database import utcnow

_secret = os.getenv("SECRET_KEY")
if not _secret:
    raise RuntimeError(
        "SECRET_KEY is not set. Generate one with:\n"
        "  python -c \"import secrets; print(secrets.token_hex(32))\"\n"
        "and add it to .env."
    )

SESSION_COOKIE_NAME = "admin_session"
SESSION_VALID_DAYS = 30
RESET_TOKEN_VALID_HOURS = 2  # short-lived on purpose -- a reset link is a real bearer credential


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except (ValueError, TypeError):
        # A malformed/corrupt stored hash should fail closed, not raise
        # past the caller and crash the login route.
        return False


def _sign(raw_value: str) -> str:
    sig = hmac.new(_secret.encode(), raw_value.encode(), hashlib.sha256).hexdigest()
    return f"{raw_value}.{sig}"


def _verify_signature(token: str) -> str | None:
    """Returns the raw (unsigned) value if the signature is valid, else None."""
    if not token or "." not in token:
        return None
    raw_value, _, sig = token.rpartition(".")
    expected = hmac.new(_secret.encode(), raw_value.encode(), hashlib.sha256).hexdigest()
    return raw_value if hmac.compare_digest(sig, expected) else None


def create_session_token(account_id: int) -> str:
    expires_at = int((utcnow() + timedelta(days=SESSION_VALID_DAYS)).timestamp())
    nonce = secrets.token_urlsafe(16)
    return _sign(f"{account_id}:{expires_at}:{nonce}")


def verify_session_token(token: str) -> int | None:
    """Returns the account id if the session token is genuine and not
    expired, else None."""
    raw = _verify_signature(token)
    if not raw:
        return None
    try:
        account_id_str, expires_at_str, _nonce = raw.split(":", 2)
    except ValueError:
        return None
    if int(expires_at_str) < utcnow().timestamp():
        return None
    return int(account_id_str)


def create_reset_token(account_id: int) -> str:
    expires_at = int((utcnow() + timedelta(hours=RESET_TOKEN_VALID_HOURS)).timestamp())
    nonce = secrets.token_urlsafe(16)
    return _sign(f"reset:{account_id}:{expires_at}:{nonce}")


def verify_reset_token(token: str) -> int | None:
    """Returns the account id if the reset token is genuine, correctly
    tagged as a reset token (not a session token reused out of scope),
    and not expired, else None."""
    raw = _verify_signature(token)
    if not raw:
        return None
    try:
        tag, account_id_str, expires_at_str, _nonce = raw.split(":", 3)
    except ValueError:
        return None
    if tag != "reset":
        return None
    if int(expires_at_str) < utcnow().timestamp():
        return None
    return int(account_id_str)
