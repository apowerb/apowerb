"""Répertoires de travail de th2agent, résolus depuis la configuration.

Avant, les chemins runtime étaient écrits en dur et en relatif un peu partout
(``"agents_pool"``, ``f"./uploads/agent{agent_id}"``, ``os.getcwd()`` pour la
toolbox). Conséquence : le paquet n'était utilisable que lancé depuis la
racine du repo — un consommateur externe se retrouvait avec des dossiers créés
dans son propre CWD, ou avec des agents introuvables.

Les valeurs par défaut reproduisent exactement l'ancien comportement (racine =
CWD, mêmes noms de dossiers), donc les déploiements existants — qui lancent
uvicorn depuis ``WORKDIR`` — ne changent pas d'un octet. Pour découpler, on pose
``TH2AGENT_RUNTIME_ROOT`` ou l'un des chemins individuels.

Portée réelle, à jour : ``agents_pool``, ``artifacts_store``, ``uploads`` et la
toolbox sont entièrement câblés — tous leurs accès disque passent par ici. Le
test ``tests/test_uploads_wiring.py`` interdit qu'un nouveau site rouvre un
chemin en dur.

Ne concerne que le **disque**. Les clés d'objet S3 (``uploads/{agent}/{fichier}``)
vivent dans un espace de noms distinct, propre au bucket : elles ne sont pas
affectées par ces réglages et ne doivent pas passer par ce module.
"""

from __future__ import annotations

from pathlib import Path

from apowerb.configs.settings import get_settings


def runtime_root() -> Path:
    """Racine des données runtime. ``TH2AGENT_RUNTIME_ROOT``, sinon le CWD."""
    configured = get_settings().runtime_root
    return Path(configured).expanduser() if configured else Path.cwd()


def _resolve(configured: str, default: str) -> Path:
    """Résout *configured* : absolu tel quel, relatif contre la racine runtime."""
    candidate = Path(configured or default).expanduser()
    return candidate if candidate.is_absolute() else runtime_root() / candidate


def agents_pool_dir() -> Path:
    """Dossier des modules d'agents générés (``AGENTS_POOL_DIR``)."""
    return _resolve(get_settings().agents_pool_dir, "agents_pool")


def artifacts_store_dir() -> Path:
    """Dossier des artefacts ADK (``ARTIFACTS_STORE_DIR``)."""
    return _resolve(get_settings().artifacts_store_dir, "artifacts_store")


def uploads_dir() -> Path:
    """Dossier des fichiers uploadés (``UPLOADS_DIR``).

    Le levier avait été retiré tant que ``uploads`` restait écrit en dur dans
    dix-neuf fichiers : honorer un ``UPLOADS_DIR`` à moitié aurait fait écrire
    les trois outils câblés à l'endroit configuré pendant que les lecteurs
    continuaient de lire ``./uploads`` — un agent ne retrouvant plus ses propres
    fichiers, sans la moindre erreur. Tous les accès disque passent désormais
    par ici, donc le réglage est rétabli.
    """
    return _resolve(get_settings().uploads_dir, "uploads")


def agent_upload_dir(agent_id: str | int) -> Path:
    """Dossier d'upload d'un agent donné (ex-``./uploads/agent{id}``)."""
    return uploads_dir() / f"agent{agent_id}"


def scope_upload_dir(scope: str) -> Path:
    """Dossier d'upload d'une *portée* RAG — un ``session_id`` ou un ``agent_id``.

    Distinct de ``agent_upload_dir`` : la portée est déjà le nom de dossier
    complet (``agent12`` ou un identifiant de session), sans préfixe à ajouter.
    """
    return uploads_dir() / scope


def toolbox_dir() -> Path:
    """Racine de la toolbox MCP — où vivent ``tools.yaml`` et le binaire.

    ``TOOLBOX_DIR`` reste prioritaire (comportement historique) ; à défaut on
    retombe sur la racine runtime, c'est-à-dire le CWD comme avant.
    """
    configured = get_settings().toolbox_dir
    if configured:
        return Path(configured).expanduser()
    return runtime_root()


def ensure_runtime_dirs() -> None:
    """Crée les dossiers runtime manquants.

    Appelé au démarrage du serveur uniquement — jamais à l'import d'un module,
    sinon importer th2agent écrirait sur le disque du consommateur.
    """
    for directory in (agents_pool_dir(), artifacts_store_dir(), uploads_dir()):
        directory.mkdir(parents=True, exist_ok=True)
