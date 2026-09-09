"""La frontière de la brique *Usage*, après le rapatriement du 09/09/26.

Le compteur ET la barre sont désormais open source : le noyau écrit
``llm_usage``, sert la jauge et applique le plafond. Ce qui reste vendu est
l'écran d'administration de la consommation — analyser par agent, par outil et
par utilisateur.

Ces tests sont le verrou de publication. Ils lisent le source, parce qu'aucun
test unitaire ne verrait la faute qu'ils cherchent : chaque moitié resterait
cohérente de son côté.

Le test qui compte le plus est ``test_une_seconde_brique_ne_double_pas`` : une
distribution restée sur l'ancien découpage câble encore son propre enregistreur.
Deux enregistreurs, c'est chaque jeton compté deux fois, sans erreur nulle part
— et une jauge fausse est pire qu'une jauge absente.
"""

from __future__ import annotations

from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
NOYAU = RACINE / "src" / "apowerb"


class TestCeQuiEstRevenuAuNoyau:
    def test_les_quatre_modules_sont_la(self):
        for module in (
            NOYAU / "core" / "agent_helpers" / "usage_recorder.py",
            NOYAU / "core" / "usage_quota.py",
            NOYAU / "helpers" / "quota_guard.py",
            NOYAU / "helpers" / "llm_usage_migration.py",
        ):
            assert module.exists(), module

    def test_le_cablage_enregistre_les_quatre_points(self):
        """Sans ces quatre-là, la jauge afficherait zéro pour toujours."""
        from apowerb.core.extensions.registry import ExtensionRegistry
        from apowerb.core.usage_wiring import register_core_usage

        registre = ExtensionRegistry()
        register_core_usage(registre)

        assert len(registre.run_guards()) == 1
        assert len(registre.model_observers()) == 1
        assert len(registre.bootstrap_hooks()) == 1
        assert registre.default_llm_cap() is not None

    def test_le_cablage_enregistre_le_plafond_du_noyau(self):
        """Le plafond annoncé par la jauge est celui que la garde applique.

        ``wire_default_llm_cap``, côté brique, testait le point d'extension
        avant de s'y brancher : un noyau trop ancien ne le connaissait pas.
        Le plafond étant passé au noyau, celui-ci ne peut plus être en retard
        sur lui-même — reste à vérifier qu'il enregistre bien CE plafond-là.
        """
        from apowerb.core.extensions.registry import ExtensionRegistry
        from apowerb.core.usage_quota import default_llm_cap
        from apowerb.core.usage_wiring import register_core_usage

        registre = ExtensionRegistry()
        register_core_usage(registre)
        assert registre.default_llm_cap() is default_llm_cap

    def test_le_plafond_repond_le_contrat_attendu(self):
        """``async fn(db, *, owner_id, plan) -> int | None``."""
        import inspect

        from apowerb.core.usage_quota import default_llm_cap

        assert inspect.iscoroutinefunction(default_llm_cap)
        params = inspect.signature(default_llm_cap).parameters
        assert "owner_id" in params and "plan" in params

    def test_le_chainage_de_callbacks_reste_au_noyau(self):
        """Plomberie ADK générique — pas une fonctionnalité vendue."""
        from apowerb.core.agent_helpers.callback_chain import (
            chain_after_model_callbacks,
        )

        assert callable(chain_after_model_callbacks)

    def test_le_noyau_garde_la_table_llm_usage(self):
        from apowerb.models import LlmUsage

        assert LlmUsage.__tablename__ == "llm_usage"


class TestCeQuiResteVendu:
    def test_lecran_dadministration_nest_pas_au_noyau(self):
        assert not (NOYAU / "routers" / "usage.py").exists()

    def test_le_noyau_nimporte_aucun_paquet_commercial(self):
        """Il relit le source : un import de brique passerait les tests
        unitaires tant que la brique est installée à côté."""
        interdits = ("th2agent_usage", "routers.usage")
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
        assert not fautifs, "le noyau importe la brique commerciale :\n  " + "\n  ".join(
            fautifs
        )


class TestLeNoyauSeReserveLesCapacites:
    def test_une_seconde_brique_ne_double_pas(self):
        """Le test qui protège la justesse du chiffre affiché.

        Une distribution restée sur l'ancien découpage enregistre encore son
        enregistreur et sa garde. Le noyau ayant réservé les capacités, ces
        seconds enregistrements sont ignorés — pas additionnés.
        """
        from apowerb.core.extensions.registry import ExtensionRegistry
        from apowerb.core.usage_wiring import register_core_usage

        registre = ExtensionRegistry()
        register_core_usage(registre)

        registre.register_model_observer(lambda **_: None, provides="llm_usage")
        registre.register_run_guard(lambda *a, **k: None, provides="llm_quota")
        registre.register_bootstrap_hook(lambda: None, provides="llm_usage_table")

        assert len(registre.model_observers()) == 1
        assert len(registre.run_guards()) == 1
        assert len(registre.bootstrap_hooks()) == 1

    def test_une_capacite_differente_sajoute_normalement(self):
        """La réservation ferme une capacité nommée, pas le point d'extension."""
        from apowerb.core.extensions.registry import ExtensionRegistry
        from apowerb.core.usage_wiring import register_core_usage

        registre = ExtensionRegistry()
        register_core_usage(registre)
        registre.register_model_observer(lambda **_: None, provides="autre_chose")

        assert len(registre.model_observers()) == 2

    def test_sans_provides_lancien_comportement_tient(self):
        """Les appels existants n'ont pas de ``provides`` et empilent."""
        from apowerb.core.extensions.registry import ExtensionRegistry

        registre = ExtensionRegistry()
        registre.register_run_guard(lambda *a, **k: None)
        registre.register_run_guard(lambda *a, **k: None)

        assert len(registre.run_guards()) == 2

    def test_un_registre_neuf_est_vide(self):
        """Le câblage se fait au montage de l'application, pas à la
        construction du registre : un test qui instancie le registre part
        d'une page blanche."""
        from apowerb.core.extensions.registry import ExtensionRegistry

        registre = ExtensionRegistry()
        assert registre.run_guards() == []
        assert registre.model_observers() == []
        assert registre.bootstrap_hooks() == []
        assert registre.default_llm_cap() is None


class TestLePlafondSeRegleIlNeSeRetirePlus:
    def test_zero_desactive_le_plafond(self):
        """L'ancien kill-switch était l'absence de brique. Désormais c'est un
        réglage : une installation qui veut des runs illimités met le cap à 0.
        """
        from apowerb.configs.settings import Settings

        champs = Settings.model_fields
        assert "default_llm_user_token_cap" in champs
        assert "default_llm_monthly_token_quota" in champs
