"""hydra.core.encryption: the key comes only from HYDRA_CREDENTIAL_KEY and is never generated."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

from hydra.core.encryption import (
    CredentialKeyError,
    decrypt,
    encrypt,
    require_credential_key,
)

KEY = "HYDRA_CREDENTIAL_KEY"


def test_missing_key_raises_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv(KEY, raising=False)
    with pytest.raises(CredentialKeyError, match=KEY):
        encrypt("abc")
    with pytest.raises(CredentialKeyError, match=KEY):
        require_credential_key()
    assert list(cwd.iterdir()) == []
    assert KEY not in os.environ


@pytest.mark.parametrize("raw", ["", "   ", "not-a-fernet-key"])
def test_invalid_key_raises(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(KEY, raw)
    with pytest.raises(CredentialKeyError):
        encrypt("abc")


def test_invalid_second_key_is_named_without_its_value(monkeypatch: pytest.MonkeyPatch) -> None:
    good = Fernet.generate_key().decode()
    monkeypatch.setenv(KEY, f"{good},bogus-secret-value")
    with pytest.raises(CredentialKeyError) as exc_info:
        require_credential_key()
    assert "key 2 of 2" in str(exc_info.value)
    assert "bogus-secret-value" not in str(exc_info.value)
    assert good not in str(exc_info.value)


def test_require_accepts_explicit_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY, raising=False)
    require_credential_key(Fernet.generate_key().decode())
    with pytest.raises(CredentialKeyError):
        require_credential_key("")


def test_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY, Fernet.generate_key().decode())
    ciphertext = encrypt("abc")
    assert "abc" not in ciphertext
    assert decrypt(ciphertext) == "abc"


def test_rotation_new_comma_old(monkeypatch: pytest.MonkeyPatch) -> None:
    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()

    monkeypatch.setenv(KEY, key_a)
    under_a = encrypt("secret-a")

    monkeypatch.setenv(KEY, f"{key_b},{key_a}")
    assert decrypt(under_a) == "secret-a"
    under_b = encrypt("secret-b")
    assert Fernet(key_b.encode()).decrypt(under_b.encode()) == b"secret-b"

    monkeypatch.setenv(KEY, key_a)
    with pytest.raises(InvalidToken):
        decrypt(under_b)


def test_wrong_key_raises_invalid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY, Fernet.generate_key().decode())
    ciphertext = encrypt("abc")
    monkeypatch.setenv(KEY, Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        decrypt(ciphertext)
