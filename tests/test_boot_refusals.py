"""Tests pour B8 — refus de boot en production avec des flags dangereux.

``working_mode == "production"`` combiné avec l'un des flags suivants doit
faire échouer l'instanciation de ``Settings`` :

- ``BYPASS_AUTH=true`` (dans l'environnement process, pas settings) —
  ``auth/dependencies.py`` fabrique un faux user ADMIN quand ce flag est
  activé. En prod = prise de contrôle totale.
- ``WEBHOOK_DEV_SKIP_SIG=true`` — bypass de la vérification OIDC Google
  Pub/Sub sur le webhook Gmail.
- ``ENCRYPT_LEGACY_ON_BOOT=true`` — la migration doit être invoquée
  explicitement via la CLI en prod, pas au boot.

Chaque test instancie ``Settings`` dans un environnement contrôlé
(``monkeypatch.setenv`` + ``chdir(tmp_path)`` pour éviter la lecture du
``.env`` réel).
"""

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from th2agent.configs.settings import Settings


ENCRYPT_KEY = Fernet.generate_key().decode()


def _prod_env(**overrides) -> dict:
    base = {
        "DB_HOST": "localhost",
        "DB_NAME": "test",
        "DB_USER": "test",
        "DB_PASSWORD": "test",
        "TEST_TOKEN": "dummy",
        "ENCRYPT_KEY": ENCRYPT_KEY,
        "WORKING_MODE": "production",
        "GOOGLE_WEBHOOK_AUDIENCE": "https://example.com/webhooks/gmail",
    }
    base.update(overrides)
    return base


class TestProductionBootRefusals:
    def test_bypass_auth_in_production_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """BYPASS_AUTH=true + WORKING_MODE=production → ValueError au boot."""
        env = _prod_env(BYPASS_AUTH="true")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ValueError) as exc:
            Settings(_env_file=None)
        assert "bypass_auth" in str(exc.value).lower()

    def test_webhook_dev_skip_sig_in_production_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """WEBHOOK_DEV_SKIP_SIG=true en prod → ValueError."""
        env = _prod_env(WEBHOOK_DEV_SKIP_SIG="true")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ValueError) as exc:
            Settings(_env_file=None)
        assert "webhook_dev_skip_sig" in str(exc.value).lower()

    def test_encrypt_legacy_on_boot_in_production_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """ENCRYPT_LEGACY_ON_BOOT=true en prod → ValueError."""
        env = _prod_env(ENCRYPT_LEGACY_ON_BOOT="true")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ValueError) as exc:
            Settings(_env_file=None)
        assert "encrypt_legacy_on_boot" in str(exc.value).lower()

    def test_dev_mode_allows_all_flags(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """En dev, toutes ces combinaisons doivent continuer à fonctionner.

        Non-régression : les développeurs locaux s'appuient sur ces flags.
        """
        env = {
            "DB_HOST": "localhost",
            "DB_NAME": "test",
            "DB_USER": "test",
            "DB_PASSWORD": "test",
            "TEST_TOKEN": "dummy",
            "ENCRYPT_KEY": ENCRYPT_KEY,
            "WORKING_MODE": "development",
            "BYPASS_AUTH": "true",
            "WEBHOOK_DEV_SKIP_SIG": "true",
            "ENCRYPT_LEGACY_ON_BOOT": "true",
        }
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        monkeypatch.chdir(tmp_path)

        # No exception: dev is the escape hatch
        settings = Settings(_env_file=None)
        assert settings.working_mode == "development"
        assert settings.webhook_dev_skip_sig is True
        assert settings.encrypt_legacy_on_boot is True
