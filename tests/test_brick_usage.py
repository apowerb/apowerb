"""Le noyau doit tourner sans la brique d'usage, et être plafonné avec elle.

Deuxième brique détachée. La ligne *Usage* du tableau d'offres est marquée
``No`` en open source, et les quotas y sont annoncés illimités : **l'absence de
plafond n'est donc pas un mode dégradé, c'est l'offre**. C'est ce que ces tests
verrouillent, parce que c'est exactement le genre de nuance qu'une relecture
future prendrait pour un oubli à « corriger ».

Note de périmètre : « Logging + insights », l'autre ligne marquée ``No``, n'est
pas dans ce dépôt. C'est th2pulse (déjà public) plus une page du front. Rien à
détacher ici.
"""

from __future__ import annotations

from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
NOYAU = RACINE / "src" / "apowerb"


class TestLeNoyauNeConnaitPlusLaBrique:
    def test_aucun_module_du_noyau_ne_nomme_l_usage(self):
        """Le verrou de publication. Il relit le source, parce qu'aucun test
        unitaire ne verrait cette faute : chaque moitié resterait cohérente."""
        interdits = (
            "quota_guard",
            "usage_quota",
            "usage_recorder",
            "llm_usage_migration",
            "routers.usage",
            "th2agent_usage",
        )
        fautifs = []
        for fichier in NOYAU.rglob("*.py"):
            for ligne_no, ligne in enumerate(
                fichier.read_text(encoding="utf-8").splitlines(), 1
            ):
                nue = ligne.strip()
                if not nue.startswith(("import ", "from ")):
                    continue
                if any(motif in nue for motif in interdits):
                    fautifs.append(f"{fichier.relative_to(NOYAU)}:{ligne_no}: {nue}")
        assert not fautifs, "le noyau importe la brique commerciale :\n  " + "\n  ".join(fautifs)

    def test_les_modules_ont_quitte_le_noyau(self):
        for parti in (
            NOYAU / "routers" / "usage.py",
            NOYAU / "core" / "usage_quota.py",
            NOYAU / "core" / "agent_helpers" / "usage_recorder.py",
            NOYAU / "helpers" / "quota_guard.py",
            NOYAU / "helpers" / "llm_usage_migration.py",
        ):
            assert not parti.exists(), parti

    def test_le_chainage_de_callbacks_reste_au_noyau(self):
        """Plomberie ADK générique — pas une fonctionnalité vendue."""
        from apowerb.core.agent_helpers.callback_chain import (
            chain_after_model_callbacks,
        )

        assert callable(chain_after_model_callbacks)

    def test_le_noyau_garde_la_table_llm_usage(self):
        """Même arbitrage que les colonnes ``mfa_*`` : inerte sans la brique,
        et la sortir imposerait de découper les migrations pour rien."""
        from apowerb.models import LlmUsage

        assert LlmUsage.__tablename__ == "llm_usage"


class TestSansBriqueToutPasse:
    def test_aucune_garde_donc_aucun_plafond(self):
        """L'offre open source annonce des quotas illimités."""
        from apowerb.core.extensions.registry import ExtensionRegistry

        assert ExtensionRegistry().run_guards() == []

    def test_aucun_observateur_donc_aucune_comptabilisation(self):
        from apowerb.core.extensions.registry import ExtensionRegistry

        assert ExtensionRegistry().model_observers() == []

    def test_aucun_crochet_de_demarrage(self):
        from apowerb.core.extensions.registry import ExtensionRegistry

        assert ExtensionRegistry().bootstrap_hooks() == []
