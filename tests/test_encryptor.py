"""Tests for src/apowerb/helpers/encryptor.py — round-trip and missing key."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

import apowerb.helpers.encryptor as encryptor_module
from apowerb.helpers.encryptor import (
    encrypt_value,
    decrypt_value,
    _build_fernet,
)


class TestRoundTrip:
    def test_encrypt_then_decrypt_returns_original(self, monkeypatch):
        """Chiffrer puis déchiffrer doit redonner la valeur d'origine."""
        key = Fernet.generate_key()
        monkeypatch.setattr(encryptor_module, "fernet", Fernet(key))

        original = "mon_secret_api_key"
        encrypted = encrypt_value(original)
        decrypted = decrypt_value(encrypted)

        assert decrypted == original

    def test_encrypted_value_is_different_from_original(self, monkeypatch):
        """La valeur chiffrée ne doit pas être identique à l'originale."""
        key = Fernet.generate_key()
        monkeypatch.setattr(encryptor_module, "fernet", Fernet(key))

        original = "mon_secret"
        encrypted = encrypt_value(original)

        assert encrypted != original


class TestMissingKey:
    def test_encrypt_raises_runtime_error_when_fernet_is_none(self, monkeypatch):
        """encrypt_value doit lever RuntimeError si fernet est None."""
        monkeypatch.setattr(encryptor_module, "fernet", None)

        with pytest.raises(RuntimeError):
            encrypt_value("hello")

    def test_decrypt_raises_runtime_error_when_fernet_is_none(self, monkeypatch):
        """decrypt_value doit lever RuntimeError si fernet est None."""
        monkeypatch.setattr(encryptor_module, "fernet", None)

        with pytest.raises(RuntimeError):
            decrypt_value("gAAAAABh...")


class TestBuildFernet:
    def test_invalid_key_returns_none(self, monkeypatch):
        """_build_fernet() doit retourner None si la clé est invalide."""
        monkeypatch.setattr(
            "apowerb.helpers.encryptor.get_settings",
            lambda: type("S", (), {"encrypt_key": "cle_invalide"})(),
        )
        result = _build_fernet()
        assert result is None

    def test_missing_key_returns_none(self, monkeypatch):
        """_build_fernet() doit retourner None si la clé est absente."""
        monkeypatch.setattr(
            "apowerb.helpers.encryptor.get_settings",
            lambda: type("S", (), {"encrypt_key": None})(),
        )
        result = _build_fernet()
        assert result is None