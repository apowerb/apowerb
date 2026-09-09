"""Le câblage du démarrage : ce qui relie l'ENTRYPOINT au code.

Trois choses qui ne cassent bruyamment nulle part ailleurs. Un script console
renommé, un ENTRYPOINT qui appelle autre chose, un `set -e` ajouté par
prudence : aucun n'échoue à la construction de l'image, aucun n'échoue aux
tests, et le premier symptôme est un conteneur qui ne démarre plus — ou qui
démarre en ignorant silencieusement toute la configuration posée.
"""

from __future__ import annotations

import pathlib
import tomllib

import pytest

RACINE = pathlib.Path(__file__).resolve().parents[1]
ENTRYPOINT = RACINE / "docker" / "entrypoint.sh"
DOCKERFILE = RACINE / "Dockerfile"
PYPROJECT = RACINE / "pyproject.toml"

# Le nom que l'ENTRYPOINT appelle. Il vit ici pour que le renommer oblige à
# passer par ce fichier, donc à voir les trois endroits qu'il faut changer
# ensemble.
SCRIPT_DEMARRAGE = "apowerb-config-env"


@pytest.fixture(scope="module")
def scripts() -> dict[str, str]:
    return tomllib.loads(PYPROJECT.read_text())["project"]["scripts"]


def test_le_script_de_demarrage_est_declare(scripts):
    assert scripts.get(SCRIPT_DEMARRAGE) == "apowerb.cli.config:app"


def test_le_script_de_demarrage_ne_passe_pas_par_la_cli_complete(scripts):
    """`apowerb.cli.main` importe uvicorn et les trois autres sous-applications
    (4,0 s mesurées, contre 0,41 s pour le seul module `config`). Le pointer
    ici rendrait à chaque démarrage de conteneur les secondes que ce script
    dédié existe précisément pour ne pas payer."""
    assert "cli.main" not in scripts[SCRIPT_DEMARRAGE]


def test_l_entrypoint_appelle_le_script_declare():
    """Le lien que rien d'autre ne vérifie : ni la construction de l'image, ni
    les tests, ni le lint ne rapprochent ces deux fichiers."""
    assert SCRIPT_DEMARRAGE in ENTRYPOINT.read_text()


def test_l_entrypoint_passe_la_main_par_exec():
    """Sans `exec`, le serveur n'est pas PID 1 : il ne reçoit plus SIGTERM
    directement, et l'arrêt de chaque pod attend le délai de grâce complet."""
    assert 'exec "$@"' in ENTRYPOINT.read_text()


def test_l_entrypoint_ne_peut_pas_interrompre_le_demarrage():
    """`set -e` ajouté par prudence transformerait une base injoignable au
    démarrage — le cas normal quand elle démarre en parallèle — en conteneur
    qui refuse de démarrer."""
    lignes = [
        ligne.strip()
        for ligne in ENTRYPOINT.read_text().splitlines()
        if not ligne.strip().startswith("#")
    ]
    assert not [ligne for ligne in lignes if ligne in {"set -e", "set -eu", "set -euo pipefail"}]


def test_le_dockerfile_installe_l_entrypoint_et_garde_son_cmd():
    """Le CMD est ce que le chart et les deux composes passent tel quel ; le
    remplacer par un ENTRYPOINT qui l'absorberait casserait les trois."""
    contenu = DOCKERFILE.read_text()
    assert 'ENTRYPOINT ["/usr/local/bin/apowerb-entrypoint"]' in contenu
    assert 'CMD ["apowerb", "serve", "--host", "0.0.0.0", "--port", "8000", "--no-reload"]' in contenu
    assert "COPY docker/entrypoint.sh /usr/local/bin/apowerb-entrypoint" in contenu


def test_l_entrypoint_est_dans_le_contexte_de_construction():
    """`.dockerignore` exclut déjà `tests/` et `.env*` : y ajouter `docker/`
    ferait échouer la construction sur un `COPY` introuvable, mais seulement
    au moment de construire l'image."""
    ignores = (RACINE / ".dockerignore").read_text().splitlines()
    motifs = {ligne.strip().strip("/") for ligne in ignores if ligne.strip() and not ligne.startswith("#")}
    assert "docker" not in motifs
