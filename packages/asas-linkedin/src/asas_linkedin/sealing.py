"""Sealing a Page token at rest. Pure.

AES-256-GCM from ``cryptography``, a fresh 96-bit nonce per seal, stored as
nonce followed by ciphertext and tag. The ``context`` is bound into the
authenticated data, so a token copied to another row or swapped between
columns fails to open: a host passes something like
``f"linkedin_page_connection:{tenant_id}:access"``. The key is 32 random
bytes, base64, from the host's secret store; the host stores a key id beside
every sealed value so the key can be rotated without guessing.
"""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import LinkedInPageNotConnectedError, LinkedInUnconfiguredError

_NONCE_BYTES = 12


def load_key(encoded: str) -> AESGCM:
    """The AES-256 key from its base64 setting, or unconfigured when absent
    or wrong."""
    try:
        raw = base64.b64decode(encoded or "", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LinkedInUnconfiguredError("the sealing key is not valid base64") from exc
    if len(raw) != 32:
        raise LinkedInUnconfiguredError("the sealing key must be 32 bytes")
    return AESGCM(raw)


def seal(key: AESGCM, plaintext: str, *, context: str) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + key.encrypt(nonce, plaintext.encode(), context.encode())


def unseal(key: AESGCM, sealed: bytes, *, context: str) -> str:
    """The token, or ``linkedin_page_not_connected`` when it will not open
    (a rotated-away key, a tampered row, the wrong context): either way the
    Page must be reconnected, and nothing about the token is said."""
    try:
        return key.decrypt(sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:], context.encode()).decode()
    except (InvalidTag, ValueError) as exc:
        raise LinkedInPageNotConnectedError("The stored LinkedIn Page token cannot be read") from exc
