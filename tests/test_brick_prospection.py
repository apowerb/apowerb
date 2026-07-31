"""Le noyau doit tourner sans la brique de prospection.

Quatrième brique, la plus grosse (4 763 lignes de source) et la première à
exercer *tous* les points d'extension : routeurs, pack d'outils, templates.

Le piège de cette brique : ``tools_store/portfolio/marketing.py`` a un nom qui
évoque le commercial mais expose les leads HubSpot du super-agent
``email_marketing_agent``, qui est basic. Le sortir aurait cassé un template du
catalogue open source. Un test le garde nommément — même famille de piège que la
confusion connexion/intégrations sur OAuth.
"""

from __future__ import annotations

from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
NOYAU = RACINE / "src" / "apowerb"


class TestLeNoyauNeConnaitPlusLaBrique:
    def test_aucun_module_du_noyau_ne_nomme_la_prospection(self):
        interdits = (
            "th2agent_prospection",
            "prospection_store",
            "prospection_email",
            "prospection_icp",
            "routers.prospection",
            "routers.campaigns",
            "templates.prospecting",
            "templates.th2prospect",
            "portfolio.prospection_",
            "campaign_tracker",
            "followup_tracker",
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
        assert not fautifs, "le noyau importe la brique :\n  " + "\n  ".join(fautifs)

    def test_les_modules_ont_quitte_le_noyau(self):
        for parti in (
            NOYAU / "routers" / "prospection.py",
            NOYAU / "routers" / "campaigns.py",
            NOYAU / "prospection_store.py",
            NOYAU / "prospection_email.py",
            NOYAU / "prospection_icp.py",
            NOYAU / "core" / "superagents" / "templates" / "prospecting.py",
            NOYAU / "core" / "superagents" / "templates" / "th2prospect.py",
        ):
            assert not parti.exists(), parti

    def test_les_outils_de_prospection_ont_quitte_le_portfolio(self):
        portfolio = NOYAU / "tools_store" / "portfolio"
        restants = sorted(
            p.name
            for p in portfolio.glob("*.py")
            if p.name.startswith("prospection_")
            or p.name in {"campaign_tracker.py", "followup_tracker.py"}
        )
        assert not restants, restants


class TestCeQuiRestVolontairementAuNoyau:
    def test_marketing_hubspot_reste_dans_le_noyau(self):
        """Le piège de cette brique.

        ``portfolio/marketing.py`` porte un nom qui évoque le commercial, mais
        c'est l'outil HubSpot du super-agent ``email_marketing_agent``, qui est
        basic. Le sortir casserait un template du catalogue open source.
        """
        module = NOYAU / "tools_store" / "portfolio" / "marketing.py"
        assert module.exists()
        assert "tool_hubspot_get_sales_leads" in module.read_text(encoding="utf-8")

    def test_le_catalogue_du_noyau_ne_nomme_aucun_template_commercial(self):
        from apowerb.core.superagents.templates import SUPERAGENT_TEMPLATES

        ids = {t["template_id"] for t in SUPERAGENT_TEMPLATES}
        assert "prospecting_loop_agent" not in ids
        assert "th2prospect_outbound" not in ids
        # Le catalogue basic n'est pas vide pour autant.
        assert "rag_agent" in ids
        assert "email_marketing_agent" in ids
