"""Le noyau doit tourner sans la brique de facturation.

Troisième brique détachée, et la plus simple : une seule couture. L'intérêt de
ce fichier n'est donc pas la difficulté du découpage, c'est de verrouiller une
nuance facile à « corriger » par erreur — voir le dernier test.
"""

from __future__ import annotations

from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
NOYAU = RACINE / "src" / "th2agent"


class TestLeNoyauNeConnaitPlusLaBrique:
    def test_aucun_module_du_noyau_ne_nomme_la_facturation(self):
        interdits = ("th2agent.billing", "th2agent_billing", "stripe_service")
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
        assert not fautifs, "le noyau importe la brique :\n  " + "\n  ".join(fautifs)

    def test_le_paquet_billing_a_quitte_le_noyau(self):
        restants = sorted(p.name for p in (NOYAU / "billing").glob("*.py"))
        assert not restants, f"sources de facturation encore dans le noyau : {restants}"

    def test_le_noyau_garde_les_tables_de_facturation(self):
        """Même arbitrage que les colonnes ``mfa_*`` et la table ``llm_usage``."""
        from th2agent.models import User

        for colonne in ("credits", "stripe_customer_id"):
            assert hasattr(User, colonne), colonne


class TestSansBriqueLaCleDisparait:
    def test_config_n_annonce_pas_billing_enabled(self):
        """La nuance à ne pas « corriger ».

        ``billing_enabled`` était en dur dans ``routers/config.py``. Sans la
        brique, la clé doit **disparaître** de la réponse, pas valoir ``False`` :
        une clé absente dit que la fonctionnalité n'existe pas dans cette
        édition, une clé fausse dit qu'elle existe et qu'elle est éteinte. Le
        front n'en fait pas la même chose.
        """
        from th2agent.core.extensions.registry import ExtensionRegistry

        assert "billing_enabled" not in ExtensionRegistry().feature_flags()

    def test_le_registre_neuf_n_a_aucun_drapeau(self):
        from th2agent.core.extensions.registry import ExtensionRegistry

        assert ExtensionRegistry().feature_flags() == {}
