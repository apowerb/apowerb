"""``uploads`` doit rester câblé sur ``configs/paths.py`` — partout, sans exception.

Ce dossier a déjà été rendu configurable une fois, puis le levier a été retiré :
trois outils passaient par ``agent_upload_dir()`` pendant que dix-neuf fichiers
ouvraient ``./uploads`` en dur. Un ``UPLOADS_DIR`` posé dans cet état aurait fait
écrire les uns à l'endroit configuré et lire les autres dans le CWD — un agent ne
retrouvant plus les fichiers qu'il vient de déposer, sans la moindre erreur.

Les tests unitaires ne peuvent pas attraper ça : chacun passe, c'est leur *somme*
qui est incohérente. D'où ce test, qui lit le source plutôt que de l'exécuter. Il
sert surtout au code qui arrivera après — un nouvel outil écrit sur l'ancien
modèle est le mode de rechute le plus probable.

**Le disque seulement.** Les clés d'objet S3 (``uploads/{agent}/{fichier}``)
vivent dans l'espace de noms du bucket, pas sur le disque local : elles ne sont
pas concernées par ``UPLOADS_DIR`` et ne doivent surtout pas être converties.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from th2agent.configs import paths
from th2agent.configs.settings import get_settings

SRC = Path(__file__).resolve().parents[1] / "src" / "th2agent"

# Le module qui définit les chemins a évidemment le droit de nommer le dossier.
FICHIERS_AUTORISÉS = {"configs/paths.py", "configs/settings.py"}

# Motifs qui construisent un chemin *disque* à partir du littéral.
MOTIFS_DISQUE = re.compile(
    r"""os\.path\.join\(\s*["']uploads["']   # os.path.join("uploads", …)
      | Path\(\s*["']uploads["']\s*\)        # Path("uploads") / …
      | ^\s*[A-Z_]+\s*=\s*["']uploads["']    # UPLOAD_DIR = "uploads"
    """,
    re.VERBOSE | re.MULTILINE,
)


def _sites_en_dur() -> list[str]:
    trouvés = []
    for fichier in sorted(SRC.rglob("*.py")):
        rel = fichier.relative_to(SRC).as_posix()
        if rel in FICHIERS_AUTORISÉS:
            continue
        for num, ligne in enumerate(fichier.read_text(encoding="utf-8").splitlines(), 1):
            if MOTIFS_DISQUE.search(ligne):
                trouvés.append(f"{rel}:{num}: {ligne.strip()}")
    return trouvés


class TestAucunCheminEnDur:
    def test_tous_les_accès_disque_passent_par_configs_paths(self):
        sites = _sites_en_dur()
        assert not sites, (
            "Ces lignes construisent un chemin d'upload sans passer par "
            "th2agent.configs.paths — utiliser uploads_dir(), agent_upload_dir() "
            "ou scope_upload_dir() :\n  " + "\n  ".join(sites)
        )

    def test_les_clés_s3_ne_sont_pas_converties(self):
        """Garde-fou inverse : convertir une clé S3 casserait la lecture du bucket.

        Un objet déposé sous ``uploads/agent12/x.pdf`` reste lisible sous cette
        clé quel que soit ``UPLOADS_DIR`` — les deux espaces de noms sont
        indépendants. Ce test échoue si quelqu'un « finit le travail » un peu
        trop loin.
        """
        clés = [
            ligne
            for fichier in SRC.rglob("*.py")
            for ligne in fichier.read_text(encoding="utf-8").splitlines()
            if re.search(r'(s3_key|prefix)\s*=\s*f?"uploads/', ligne)
        ]
        assert clés, "plus aucune clé S3 littérale : suspect, elles ne doivent pas bouger"


class TestLeLevierEstHonoré:
    @pytest.fixture
    def racine_configurée(self, monkeypatch, tmp_path):
        monkeypatch.setattr(get_settings(), "runtime_root", str(tmp_path), raising=False)
        return tmp_path

    def test_uploads_suit_la_racine_runtime(self, racine_configurée):
        assert paths.uploads_dir() == racine_configurée / "uploads"
        assert paths.agent_upload_dir(42) == racine_configurée / "uploads" / "agent42"
        assert paths.scope_upload_dir("sess-1") == racine_configurée / "uploads" / "sess-1"

    def test_uploads_dir_absolu_ignore_la_racine(self, monkeypatch, tmp_path):
        monkeypatch.setattr(get_settings(), "runtime_root", "/ne/doit/pas/servir", raising=False)
        monkeypatch.setattr(get_settings(), "uploads_dir", str(tmp_path / "ailleurs"), raising=False)
        assert paths.uploads_dir() == tmp_path / "ailleurs"

    def test_le_défaut_reproduit_le_comportement_historique(self, monkeypatch):
        """Les déploiements existants lancent uvicorn depuis la racine du repo."""
        monkeypatch.setattr(get_settings(), "runtime_root", "", raising=False)
        monkeypatch.setattr(get_settings(), "uploads_dir", "uploads", raising=False)
        assert paths.uploads_dir() == Path.cwd() / "uploads"

    def test_le_chemin_n_est_pas_figé_à_l_import(self, monkeypatch, tmp_path):
        """Le défaut de `routers/artifacts.py` : une constante de module.

        Le symptôme n'apparaît que chez un consommateur dont le CWD diffère au
        moment de l'import — donc jamais en CI, jamais en déploiement, et
        systématiquement chez lui.
        """
        from th2agent.core import knowledge_map

        monkeypatch.setattr(get_settings(), "runtime_root", str(tmp_path / "a"), raising=False)
        premier = knowledge_map._map_path("agent1")

        monkeypatch.setattr(get_settings(), "runtime_root", str(tmp_path / "b"), raising=False)
        second = knowledge_map._map_path("agent1")

        assert premier != second, "le chemin de la knowledge map est figé à l'import"


class TestLesLecteursEtLesÉcrivainsSAccordent:
    """Le vrai risque n'est pas qu'un chemin soit faux, c'est qu'ils divergent."""

    @pytest.fixture(autouse=True)
    def _racine(self, monkeypatch, tmp_path):
        monkeypatch.setattr(get_settings(), "runtime_root", str(tmp_path), raising=False)
        self.racine = tmp_path

    def test_le_webhook_dépose_là_où_l_outil_de_lecture_cherche(self):
        """Symétrie exacte : ces deux-là se sont déjà désalignés en prod."""
        from th2agent.core import knowledge_map

        attendu = self.racine / "uploads" / "agent7"
        assert paths.agent_upload_dir(7) == attendu
        assert Path(knowledge_map._map_path("agent7")).parent == attendu

    def test_le_stockage_local_écrit_sous_la_racine_configurée(self, monkeypatch):
        from th2agent.storage.storage_service import StorageService

        monkeypatch.setattr(get_settings(), "storage_mode", "local", raising=False)

        service = StorageService()
        service.upload_bytes_to_storage(b"x", "agent7/fichier.txt")

        assert (self.racine / "uploads" / "agent7" / "fichier.txt").read_bytes() == b"x"

    def test_un_agent_relit_le_fichier_qu_il_vient_d_ecrire(self, monkeypatch):
        """L'aller-retour réel — le scénario que le demi-câblage cassait.

        ``create_downloadable_file`` écrit, ``read_uploaded_file`` relit. Ce sont
        deux modules distincts ; tant que l'un passait par ``agent_upload_dir()``
        et l'autre par ``./uploads``, l'agent recevait « File not found » sur un
        fichier qu'il venait lui-même de produire, avec une racine configurée.
        """
        from th2agent.core.agent_helpers.read_file_tool import _make_read_uploaded_file
        from th2agent.core.agent_helpers.tool_factories import _make_create_downloadable_file

        monkeypatch.setattr(get_settings(), "storage_mode", "local", raising=False)

        écrire = _make_create_downloadable_file("agent7")
        lire = _make_read_uploaded_file("agent7")

        écrit = écrire("note.md", "bonjour")
        assert écrit["status"] == "success", écrit

        # Le fichier atterrit bien sous la racine configurée, pas dans le CWD.
        assert (self.racine / "uploads" / "agent7" / "note.md").exists()

        relu = lire("note.md")
        assert relu["status"] == "success", relu
        assert "bonjour" in relu["content"]
