"""Une brique doit se brancher sans que le noyau la nomme.

C'est la condition pour publier le noyau seul : le code commercial ne doit pas
être présent-mais-désactivé par un drapeau, il doit être **absent** du dépôt et
se rebrancher en se déclarant dans ``TH2_EXTENSIONS``.

Le mécanisme existait déjà pour l'overlay client SCEI (``TH2_OVERLAY_MODULE``,
un seul module, callbacks/gates/webhooks). Ces tests couvrent ce qui manquait
pour découper le produit lui-même : plusieurs extensions à la fois, des packs
d'outils résolus hors du paquet du noyau, et des routeurs HTTP montés sans être
importés nommément.

Ce qui est vérifié ici est la **propriété d'absence** : on ne teste pas qu'une
option marche, on teste que le noyau se comporte correctement quand le code de
la brique n'existe pas, puis qu'il l'intègre quand elle apparaît.
"""

from __future__ import annotations

import sys
import types

import pytest
from fastapi import APIRouter

from apowerb.core.extensions import loader
from apowerb.core.extensions.registry import CORE_TOOL_PACK, ExtensionRegistry


@pytest.fixture
def registre():
    """Un registre neuf — jamais le singleton, qui est partagé par le process."""
    return ExtensionRegistry()


@pytest.fixture
def brique(monkeypatch):
    """Fabrique un module d'extension jetable, injecté dans ``sys.modules``."""
    créés: list[str] = []

    def _fabriquer(nom: str, point_entrée: str = "register", **attributs):
        mod = types.ModuleType(nom)
        for clé, valeur in attributs.items():
            setattr(mod, clé, valeur)
        monkeypatch.setitem(sys.modules, nom, mod)
        créés.append(nom)
        return mod

    return _fabriquer


class TestLeNoyauSeulSuffit:
    def test_aucune_variable_aucune_extension(self, monkeypatch):
        monkeypatch.delenv("TH2_EXTENSIONS", raising=False)
        monkeypatch.delenv("TH2_OVERLAY_MODULE", raising=False)
        assert loader.load_extensions() == []

    def test_le_pack_du_noyau_est_deja_la(self, registre):
        """Sans configuration, la résolution d'outils marche comme avant."""
        assert registre.tool_packs() == [CORE_TOOL_PACK]

    def test_aucun_routeur_par_defaut(self, registre):
        assert registre.routers() == []


class TestPlusieursBriques:
    """Le manque qui bloquait le découpage : l'ancien loader n'en gérait qu'une.

    Un client a besoin de son overlay *et* du pack commercial en même temps.
    """

    def test_deux_extensions_sont_chargees_dans_l_ordre(self, monkeypatch, brique):
        vus: list[str] = []
        brique("brique_a", register=lambda reg: vus.append("a"))
        brique("brique_b", register=lambda reg: vus.append("b"))
        monkeypatch.setenv("TH2_EXTENSIONS", "brique_a, brique_b")
        monkeypatch.delenv("TH2_OVERLAY_MODULE", raising=False)

        assert loader.load_extensions() == ["brique_a", "brique_b"]
        assert vus == ["a", "b"], "l'ordre d'enregistrement doit être déterministe"

    def test_l_ancienne_variable_reste_honoree(self, monkeypatch, brique):
        """L'overlay SCEI tourne en production avec ``init_overlay`` — le renommer
        le casserait au déploiement, pas en test."""
        brique("overlay_legacy", init_overlay=lambda reg: None)
        monkeypatch.delenv("TH2_EXTENSIONS", raising=False)
        monkeypatch.setenv("TH2_OVERLAY_MODULE", "overlay_legacy")

        assert loader.load_overlay() == "overlay_legacy"

    def test_une_brique_sans_point_d_entree_echoue_bruyamment(self, monkeypatch, brique):
        """Fail-fast : une extension cassée doit tomber au healthcheck, pas
        dégrader le service en silence."""
        brique("brique_muette", valeur=1)
        monkeypatch.setenv("TH2_EXTENSIONS", "brique_muette")
        monkeypatch.delenv("TH2_OVERLAY_MODULE", raising=False)

        with pytest.raises(RuntimeError, match="point d'entree"):
            loader.load_extensions()


class TestPacksDOutils:
    def test_une_brique_ajoute_son_pack_sans_masquer_le_noyau(self, registre):
        registre.register_tool_pack("ailleurs.schema", "ailleurs.portfolio")

        packs = registre.tool_packs()
        assert packs[0] == CORE_TOOL_PACK, "le noyau doit rester prioritaire"
        assert packs[-1].portfolio_package == "ailleurs.portfolio"

    def test_enregistrer_deux_fois_le_meme_pack_ne_le_duplique_pas(self, registre):
        registre.register_tool_pack("a.schema", "a.portfolio")
        registre.register_tool_pack("a.schema", "a.portfolio")
        assert len(registre.tool_packs()) == 2  # noyau + le pack, une seule fois

    def test_un_outil_absent_ne_fait_pas_tomber_la_construction(self, monkeypatch):
        """Le nom d'outil vient de la base. Retirer une brique laisse derrière
        des lignes qui la référencent encore — elles doivent être ignorées, pas
        faire échouer tous les agents du serveur."""
        from apowerb.tools_store import tools_helpers

        assert tools_helpers._import_from_packs(
            "module_qui_n_existe_nulle_part", attr="portfolio_package"
        ) is None

    def test_un_outil_du_noyau_se_resout_toujours(self):
        """Contre-épreuve : le test précédent ne doit pas passer parce que la
        résolution est cassée pour tout le monde."""
        from apowerb.tools_store import tools_helpers

        module = tools_helpers._import_from_packs("basic", attr="portfolio_package")
        assert module is not None
        assert module.__name__.endswith("portfolio.basic")


class TestRouteurs:
    def test_une_brique_apporte_un_routeur(self, registre):
        routeur = APIRouter()

        @routeur.get("/venu-d-ailleurs")
        def _handler():  # pragma: no cover - jamais appelé
            return {}

        registre.register_router(routeur, prefix="/api", name="brique-test")

        specs = registre.routers()
        assert len(specs) == 1
        assert specs[0].prefix == "/api"
        assert specs[0].name == "brique-test"

    def test_le_noyau_ne_nomme_aucun_module_tiers_dans_son_montage(self):
        """Le verrou qui protège la publication.

        ``main.py`` monte les routeurs de briques en itérant le registre. Si
        quelqu'un rajoute un import nommé vers un paquet hors ``apowerb``, le
        fichier ne peut plus partir en open source tel quel.
        """
        from pathlib import Path

        source = Path(loader.__file__).resolve().parents[3] / "apowerb" / "main.py"
        lignes = [
            ligne.strip()
            for ligne in source.read_text(encoding="utf-8").splitlines()
            if ligne.strip().startswith(("import ", "from "))
        ]
        étrangers = [
            ligne
            for ligne in lignes
            if ligne.startswith("from th2customers") or ligne.startswith("import th2customers")
        ]
        assert not étrangers, f"main.py nomme un paquet client : {étrangers}"
