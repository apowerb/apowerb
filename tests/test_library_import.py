"""th2agent doit être importable comme une library, sans environnement.

Historiquement, ``import th2agent.<n_importe_quoi>`` exigeait un ``.env``
complet : ~30 modules faisaient ``settings = get_settings()`` en colonne 1 et
``Settings`` déclarait 6 champs sans valeur par défaut. Importer le paquet
depuis un autre projet était donc impossible — c'était une application
déguisée en package.

Ces tests verrouillent le contrat inverse :

1. ``Settings()`` se construit sans aucune variable d'environnement ;
2. les modules du paquet s'importent dans un interpréteur vierge, sans
   ``.env``, sans base de données, sans écriture disque ;
3. la validation « il manque des variables » reste stricte, mais elle se
   déclenche au *boot du serveur*, pas à l'import.

Le point 2 est vérifié dans un **sous-processus** avec un CWD temporaire :
c'est la seule façon honnête de prouver l'absence d'effet de bord à l'import
(le processus pytest, lui, a déjà tout chargé).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from th2agent.configs.settings import RUNTIME_REQUIRED_FIELDS, Settings


# Modules représentatifs de chaque sous-système : si ceux-là s'importent à
# vide, le paquet est consommable comme library.
LIBRARY_MODULES = [
    "th2agent",
    "th2agent.configs.settings",
    "th2agent.configs.paths",
    "th2agent.helpers.security",
    "th2agent.helpers.encryptor",
    "th2agent.helpers.database",
    "th2agent.helpers.database_connection",
    "th2agent.models",
    "th2agent.agent_store.agent_manager",
    "th2agent.tools_store.tool_config",
    "th2agent.skills_store.skill_manager",
    "th2agent.storage.storage_service",
    "th2agent.integrations.helpers",
    "th2agent.users.router",
    "th2agent.core.adk_agent_builder",
    "th2agent.routers.scheduler",
]


def _run_clean(code: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """Exécute *code* dans un interpréteur vierge, hors du repo.

    L'environnement est purgé de toute variable ``DB_*``/``ENCRYPT_KEY``…
    et le CWD est un répertoire temporaire, donc aucun ``.env`` n'est lisible.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {*(f.upper() for f in RUNTIME_REQUIRED_FIELDS), "DB_PORT"}
    }
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


class TestSettingsWithoutEnv:
    def test_settings_build_without_any_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """``Settings()`` ne doit plus exploser quand rien n'est configuré."""
        for field in RUNTIME_REQUIRED_FIELDS:
            monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.chdir(tmp_path)

        settings = Settings(_env_file=None)

        assert settings.db_host == ""
        assert settings.encrypt_key == ""

    def test_missing_runtime_fields_lists_the_gaps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """La validation stricte survit : elle est juste devenue explicite."""
        for field in RUNTIME_REQUIRED_FIELDS:
            monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.chdir(tmp_path)

        settings = Settings(_env_file=None)

        assert set(settings.missing_runtime_fields()) == set(RUNTIME_REQUIRED_FIELDS)
        with pytest.raises(RuntimeError) as exc:
            settings.assert_runtime_ready()
        # Le message doit nommer les variables d'environnement, pas les champs.
        assert "DB_HOST" in str(exc.value)
        assert "ENCRYPT_KEY" in str(exc.value)

    def test_configured_settings_are_runtime_ready(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
            monkeypatch.setenv(key, "x")
        monkeypatch.setenv("TEST_TOKEN", "x")
        monkeypatch.setenv("ENCRYPT_KEY", "x")
        monkeypatch.chdir(tmp_path)

        settings = Settings(_env_file=None)

        assert settings.missing_runtime_fields() == []
        settings.assert_runtime_ready()  # ne lève pas


class TestImportIsSideEffectFree:
    def test_modules_import_in_a_clean_interpreter(self, tmp_path: Path):
        """Aucun module ne doit exiger un .env pour être importé."""
        code = "import importlib\n" + "\n".join(
            f"importlib.import_module({m!r})" for m in LIBRARY_MODULES
        )
        result = _run_clean(code, tmp_path)
        assert result.returncode == 0, (
            f"import échoué sans environnement:\n{result.stderr[-3000:]}"
        )

    def test_import_does_not_touch_the_filesystem(self, tmp_path: Path):
        """Importer ne doit créer ni agents_pool/ ni uploads/ dans le CWD."""
        code = "import importlib\n" + "\n".join(
            f"importlib.import_module({m!r})" for m in LIBRARY_MODULES
        )
        result = _run_clean(code, tmp_path)
        assert result.returncode == 0, result.stderr[-2000:]
        assert list(tmp_path.iterdir()) == [], (
            f"l'import a écrit sur le disque: {[p.name for p in tmp_path.iterdir()]}"
        )

    def test_importing_the_server_module_only_costs_the_adk_artifact_dir(
        self, tmp_path: Path
    ):
        """``th2agent.main`` s'importe sans configuration et sans migration.

        C'est le module le plus dur : il construit l'app ASGI à l'import,
        parce que le déploiement référence ``th2agent.main:app``. Il ne joue
        plus aucune migration et ne crée plus ``agents_pool/`` ni
        ``uploads/`` — cela appartient à ``bootstrap()``, au démarrage réel.

        Reste ``artifacts_store/``, créé par ``get_fast_api_app()`` de Google
        ADK au moment où il instancie son service d'artefacts fichier : ce
        n'est pas notre code, et s'en débarrasser imposerait de passer l'app
        en factory (``--factory`` côté uvicorn + systemd). On le tolère donc,
        mais nommément : toute *autre* écriture fait échouer ce test.
        """
        code = """
            import th2agent.main as m
            assert callable(m.bootstrap)
            assert m.app is not None
        """
        result = _run_clean(code, tmp_path)
        assert result.returncode == 0, result.stderr[-3000:]
        written = sorted(p.name for p in tmp_path.iterdir())
        assert written in ([], ["artifacts_store"]), (
            f"importer main a écrit autre chose que le dossier d'artefacts ADK: {written}"
        )

    def test_importing_the_scheduler_router_builds_no_orchestrator(self, tmp_path: Path):
        """``scheduler_client = get_orchestrator().client`` tournait à l'import.

        Monter ``th2agent.routers.scheduler`` sur sa propre app construisait
        donc un client HTTP d'orchestration avant la première requête, et
        figeait le choix ``mage``/``th2etl`` au moment de l'import plutôt qu'à
        celui de l'appel. Le proxy résout à l'accès d'attribut ; on vérifie ici
        qu'aucun client n'existe tant que personne ne s'en sert.
        """
        code = """
            import th2agent.scheduler.mage as mage

            appels = []
            mage.get_orchestrator = lambda *a, **k: appels.append(1)

            import th2agent.routers.scheduler as sched
            assert appels == [], "l'import a construit un orchestrateur"
            assert sched.scheduler_client is not None
        """
        result = _run_clean(code, tmp_path)
        assert result.returncode == 0, result.stderr[-3000:]

    def test_encryptor_exposes_none_when_key_is_missing(self, tmp_path: Path):
        """``encryptor.fernet`` vaut None (pas une exception) sans ENCRYPT_KEY."""
        code = """
            from th2agent.helpers import encryptor
            assert encryptor.fernet is None, encryptor.fernet
            try:
                encryptor.encrypt_value("x")
            except RuntimeError as exc:
                assert "ENCRYPT_KEY" in str(exc), exc
            else:
                raise AssertionError("encrypt_value aurait dû refuser")
        """
        result = _run_clean(code, tmp_path)
        assert result.returncode == 0, result.stderr[-2000:]
