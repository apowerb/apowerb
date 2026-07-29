"""Les répertoires runtime annoncés configurables doivent l'être partout.

`configs/paths.py` a été introduit pour que th2agent cesse de dépendre de son
répertoire courant. Mais une option de configuration qui n'est honorée que par
une partie du code est pire que pas d'option : elle ne casse rien bruyamment,
elle désaligne deux moitiés du produit en silence.

Deux cas concrets étaient dans cet état :

- ``ARTIFACTS_STORE_DIR`` : ``main.py`` le passait à ADK, qui écrivait bien à
  l'endroit configuré, pendant que ``routers/artifacts.py`` gardait une
  constante ``os.path.abspath("artifacts_store")`` — résolue contre le CWD, et
  figée à l'import. L'endpoint listait un dossier vide et répondait ``[]``.
- ``UPLOADS_DIR`` : trois outils écrivaient via ``agent_upload_dir()`` tandis
  qu'une vingtaine de fichiers lisaient ``./uploads`` en dur.

Les deux sont réparés. Le second l'a été en deux temps — levier retiré d'abord,
rétabli une fois les dix-neuf fichiers câblés — et son verrou de non-régression
vit dans ``test_uploads_wiring.py``, qui relit le source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from th2agent.configs import paths
from th2agent.configs.settings import get_settings


@pytest.fixture
def racine_configurée(monkeypatch, tmp_path):
    """Déplace la racine runtime, comme le ferait un consommateur externe."""
    monkeypatch.setattr(get_settings(), "runtime_root", str(tmp_path), raising=False)
    return tmp_path


class TestArtefacts:
    def test_l_endpoint_cherche_là_où_adk_écrit(self, racine_configurée):
        """Les deux côtés doivent résoudre la même racine, par construction."""
        from th2agent.routers import artifacts

        chemin = artifacts._get_session_artifacts_dir("agent1", "u", "s")

        assert Path(chemin) == paths.artifacts_store_dir() / "agent1" / "u" / "s"
        assert str(racine_configurée) in chemin

    def test_le_chemin_n_est_pas_figé_à_l_import(self, monkeypatch, tmp_path):
        """La constante de module capturait le CWD une fois pour toutes.

        Le symptôme n'apparaissait que chez un consommateur dont le répertoire
        courant diffère au moment de l'import — donc jamais en CI, jamais en
        déploiement, et systématiquement chez lui.
        """
        from th2agent.routers import artifacts

        monkeypatch.setattr(get_settings(), "runtime_root", str(tmp_path / "a"), raising=False)
        premier = artifacts._get_session_artifacts_dir("agent1", "u", "s")

        monkeypatch.setattr(get_settings(), "runtime_root", str(tmp_path / "b"), raising=False)
        second = artifacts._get_session_artifacts_dir("agent1", "u", "s")

        assert premier != second, "le chemin des artefacts est figé à l'import"


class TestUploads:
    def test_uploads_suit_la_racine(self, racine_configurée):
        """Rétabli une fois les dix-neuf fichiers câblés.

        Le détail de la couverture est verrouillé dans ``test_uploads_wiring.py``
        (scan du source) ; ici on ne vérifie que la résolution.
        """
        assert paths.uploads_dir() == racine_configurée / "uploads"
        assert paths.agent_upload_dir(42) == racine_configurée / "uploads" / "agent42"


class TestCeQuiEstVraimentCâblé:
    def test_les_trois_répertoires_suivent_la_racine(self, racine_configurée):
        assert paths.agents_pool_dir() == racine_configurée / "agents_pool"
        assert paths.artifacts_store_dir() == racine_configurée / "artifacts_store"
        assert paths.uploads_dir() == racine_configurée / "uploads"
