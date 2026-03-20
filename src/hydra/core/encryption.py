"""Fernet-based credential encryption for exchange API keys.

Uses the ``cryptography`` library to encrypt/decrypt sensitive credentials
at rest. The encryption key is sourced from the ``HYDRA_CREDENTIAL_KEY``
environment variable.
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

_KEY_ENV_VAR = "HYDRA_CREDENTIAL_KEY"


def get_fernet_key() -> bytes:
    """Return the Fernet encryption key from the environment.

    If ``HYDRA_CREDENTIAL_KEY`` is not set, a new key is generated and
    written to ``.env`` for persistence, with a warning logged.
    """
    raw = os.environ.get(_KEY_ENV_VAR)
    if raw:
        return raw.encode()

    # Generate a new key and persist it
    key = Fernet.generate_key()
    logger.warning(
        "No %s found in environment — generated a new key. "
        "Set this in your environment for production use.",
        _KEY_ENV_VAR,
    )

    # Attempt to persist to .env for convenience
    try:
        env_path = os.path.join(os.getcwd(), ".env")
        with open(env_path, "a") as f:
            f.write(f"\n{_KEY_ENV_VAR}={key.decode()}\n")
        logger.info("Wrote new credential key to %s", env_path)
    except OSError:
        logger.debug("Could not write key to .env file")

    os.environ[_KEY_ENV_VAR] = key.decode()
    return key


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string using Fernet. Returns base64-encoded ciphertext."""
    f = Fernet(get_fernet_key())
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted ciphertext string."""
    f = Fernet(get_fernet_key())
    return f.decrypt(ciphertext.encode()).decode()
