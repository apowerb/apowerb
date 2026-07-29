"""La clé de signature des JWT ne doit jamais valoir la chaîne vide.

Contexte. Rendre th2agent importable comme library a consisté à donner des
valeurs par défaut aux champs de configuration, dont ``encrypt_key``. Effet de
bord non voulu : ``helpers/security.py`` figeait ``SECRET_KEY`` au niveau
module, donc une ``ENCRYPT_KEY`` absente ne faisait plus planter l'import — elle
donnait silencieusement ``SECRET_KEY = ""``. Or cette valeur signe et vérifie
les JWT.

Le serveur refuse de démarrer sans la clé (``bootstrap()``), donc la production
n'était pas exposée. Mais un échec bruyant avait été remplacé par un défaut
silencieux sur un chemin de sécurité, ce qui n'est pas un troc acceptable.

Ces tests verrouillent les deux exigences, qui semblent contradictoires et ne le
sont pas :

1. importer le module sans configuration reste possible (contrat library) ;
2. *utiliser* la clé sans configuration est refusé, bruyamment.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from th2agent.configs.settings import get_settings
from th2agent.helpers import security


def _run_without_encrypt_key(code: str, tmp_path) -> subprocess.CompletedProcess:
    """Exécute *code* dans un interpréteur sans ENCRYPT_KEY ni .env lisible."""
    env = {k: v for k, v in os.environ.items() if k != "ENCRYPT_KEY"}
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


class TestCléConfigurée:
    def test_retourne_la_clé_des_settings(self):
        attendu = get_settings().encrypt_key
        if not attendu:
            pytest.skip("environnement de test sans ENCRYPT_KEY")
        assert security.get_secret_key() == attendu

    def test_les_jetons_restent_signés_et_vérifiables(self):
        if not get_settings().encrypt_key:
            pytest.skip("environnement de test sans ENCRYPT_KEY")
        token = security.create_access_token({"sub": "a@b.c"})
        payload = security.decode_access_token(token)
        assert payload["sub"] == "a@b.c"


class TestCléAbsente:
    def test_import_toujours_possible(self, tmp_path):
        """Le contrat library survit : importer ne configure rien."""
        result = _run_without_encrypt_key(
            "import th2agent.helpers.security  # ne doit pas lever", tmp_path
        )
        assert result.returncode == 0, result.stderr[-2000:]

    def test_utiliser_la_clé_est_refusé(self, tmp_path):
        code = """
            from th2agent.helpers import security

            try:
                security.get_secret_key()
            except RuntimeError as exc:
                assert "ENCRYPT_KEY" in str(exc), exc
            else:
                raise AssertionError("une clé vide aurait dû être refusée")
        """
        result = _run_without_encrypt_key(code, tmp_path)
        assert result.returncode == 0, result.stderr[-2000:]

    def test_signer_un_jeton_est_refusé(self, tmp_path):
        """Le point qui compte : aucun JWT ne peut être émis sans clé."""
        code = """
            from th2agent.helpers import security

            try:
                security.create_access_token({"sub": "x"})
            except RuntimeError as exc:
                assert "ENCRYPT_KEY" in str(exc), exc
            else:
                raise AssertionError("un jeton a été signé sans clé")
        """
        result = _run_without_encrypt_key(code, tmp_path)
        assert result.returncode == 0, result.stderr[-2000:]


class TestAncienneConstante:
    def test_secret_key_ne_revient_pas_par_la_fenêtre(self):
        """``SECRET_KEY`` a disparu, et son accès explique pourquoi.

        Laisser le nom en place, même corrigé, inviterait à le réutiliser tel
        quel — c'est-à-dire à refiger la valeur à l'import.
        """
        with pytest.raises(AttributeError) as exc:
            security.SECRET_KEY  # noqa: B018
        assert "get_secret_key" in str(exc.value)
