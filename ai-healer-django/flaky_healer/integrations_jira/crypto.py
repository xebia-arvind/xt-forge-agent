"""
Fernet symmetric encryption for Jira API tokens.

The key is provided by `settings.FERNET_KEY`. In development the key falls back
to a value derived from SECRET_KEY so migrations do not fail. In production set
the env var `FERNET_KEY` to a base64-urlsafe 32-byte key (generate with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
"""
from __future__ import annotations

from functools import lru_cache

from django.conf import settings


@lru_cache(maxsize=1)
def _cipher():
    # Local import so module is safe to load before cryptography is installed
    # (django-admin check runs before pip install in some flows).
    from cryptography.fernet import Fernet

    key = getattr(settings, "FERNET_KEY", "")
    if not key:
        raise RuntimeError("FERNET_KEY is not configured; refusing to encrypt/decrypt Jira tokens.")
    if isinstance(key, str):
        key = key.encode("utf-8")
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    if plaintext is None:
        plaintext = ""
    return _cipher().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _cipher().decrypt(ciphertext.encode("ascii")).decode("utf-8")
