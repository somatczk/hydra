"""Fernet encryption for credentials stored in the database.

The key comes only from ``HYDRA_CREDENTIAL_KEY``: one Fernet key, or a comma-separated list for
rotation (``new,old``: the first key encrypts, every key decrypts). A key is never generated or
written anywhere; a missing or malformed key raises :class:`CredentialKeyError`. ``decrypt`` lets
``cryptography.fernet.InvalidToken`` through so callers can report undecryptable rows.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, MultiFernet

KEY_ENV_VAR = "HYDRA_CREDENTIAL_KEY"


class CredentialKeyError(RuntimeError):
    """HYDRA_CREDENTIAL_KEY is missing or is not a valid Fernet key list."""


def _parse(raw: str | None) -> MultiFernet:
    if raw is None or not raw.strip():
        raise CredentialKeyError(
            f"{KEY_ENV_VAR} is not set; it is required whenever DATABASE_URL is set"
        )
    parts = [part.strip() for part in raw.split(",")]
    fernets: list[Fernet] = []
    for index, part in enumerate(parts, start=1):
        try:
            fernets.append(Fernet(part.encode()))
        except ValueError:
            raise CredentialKeyError(
                f"{KEY_ENV_VAR}: key {index} of {len(parts)} is not a valid Fernet key"
            ) from None
    return MultiFernet(fernets)


def get_cipher() -> MultiFernet:
    """The cipher for the current ``HYDRA_CREDENTIAL_KEY``."""
    return _parse(os.environ.get(KEY_ENV_VAR))


def require_credential_key(raw: str | None = None) -> None:
    """Fail fast at startup: validate *raw*, or ``HYDRA_CREDENTIAL_KEY`` when not given."""
    _parse(raw if raw is not None else os.environ.get(KEY_ENV_VAR))


def encrypt(plaintext: str) -> str:
    """Encrypt with the first key. Returns the Fernet token as text."""
    return get_cipher().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt with any configured key; raises ``InvalidToken`` when none matches."""
    return get_cipher().decrypt(ciphertext.encode()).decode()
