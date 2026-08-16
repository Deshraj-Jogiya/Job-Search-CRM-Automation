"""
Encryption helper for sensitive stored values (e.g. portal login passwords,
if/when account auto-registration is added back in a later phase).

SETUP:
1. pip install cryptography
2. Generate a key once:
       python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
3. Put it in .env as CREDENTIAL_ENCRYPTION_KEY=<key>
"""

import os
from cryptography.fernet import Fernet, InvalidToken

_key = os.getenv("CREDENTIAL_ENCRYPTION_KEY")
_fernet = Fernet(_key.encode()) if _key else None


def _require_key():
    if _fernet is None:
        raise RuntimeError(
            "CREDENTIAL_ENCRYPTION_KEY is not set. Generate one with:\n"
            "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
            "and add it to .env."
        )


def encrypt_value(plaintext: str) -> str:
    if plaintext is None:
        return None
    _require_key()
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_value(token: str) -> str:
    if token is None:
        return None
    _require_key()
    try:
        return _fernet.decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError(
            "Could not decrypt stored credential -- wrong key, or it was "
            "encrypted under a rotated key."
        ) from e
