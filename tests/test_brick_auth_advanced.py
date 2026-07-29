"""Le noyau doit être complet sans la brique d'auth avancée, et complété par elle.

Première brique réellement détachée. Ce qui est vérifié n'est pas qu'une option
fonctionne, mais deux propriétés symétriques :

1. **Absence** — sans la brique, le noyau démarre, la connexion par email et mot
   de passe marche de bout en bout, et les routes commerciales n'existent pas.
   C'est la condition pour publier le dépôt : le code n'est pas désactivé par un
   drapeau, il n'est pas là.
2. **Rattachement** — avec la brique, les routes réapparaissent et le second
   facteur reprend la main au bon endroit du flux de connexion.

Le piège à ne pas rouvrir : OAuth Google/Microsoft sert *aussi* aux intégrations
(brancher Drive, Gmail, Outlook comme outils d'un agent). Celles-là restent dans
le noyau. Un test les garde explicitement.
"""

from __future__ import annotations

import importlib
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
NOYAU = RACINE / "src" / "th2agent"


class TestLeNoyauNeConnaitPlusLaBrique:
    def test_aucun_module_du_noyau_ne_nomme_l_auth_avancee(self):
        """Le verrou. S'il tombe, le dépôt ne peut plus partir en open source."""
        interdits = ("users.oauth", "auth.mfa_service", "th2agent_auth_advanced")
        fautifs = []
        for fichier in NOYAU.rglob("*.py"):
            texte = fichier.read_text(encoding="utf-8")
            for ligne_no, ligne in enumerate(texte.splitlines(), 1):
                nue = ligne.strip()
                if not nue.startswith(("import ", "from ")):
                    continue
                if any(motif in nue for motif in interdits):
                    fautifs.append(f"{fichier.relative_to(NOYAU)}:{ligne_no}: {nue}")
        assert not fautifs, "le noyau importe la brique commerciale :\n  " + "\n  ".join(fautifs)

    def test_les_modules_deplaces_ont_bien_quitte_le_noyau(self):
        """On teste l'absence de *sources*, pas de répertoires.

        Un ``__pycache__`` orphelin fait exister ``users/oauth/`` longtemps
        après le déplacement — il n'est pas suivi par git, donc il survit à un
        ``reset --hard``. Le faire échouer là-dessus serait un faux positif qui
        userait la confiance dans ce fichier.
        """
        restants = sorted(p.name for p in (NOYAU / "users" / "oauth").glob("*.py"))
        assert not restants, f"sources OAuth encore dans le noyau : {restants}"
        assert not (NOYAU / "auth" / "mfa_service.py").exists()

    def test_le_noyau_garde_les_colonnes_mfa(self):
        """Décision assumée : inertes sans la brique, les sortir imposerait de
        découper les migrations pour un gain nul."""
        from th2agent.models import User

        for colonne in ("mfa_enabled", "mfa_secret", "mfa_backup_codes"):
            assert hasattr(User, colonne), colonne


class TestLeNoyauResteComplet:
    def test_la_connexion_par_mot_de_passe_ne_depend_d_aucun_crochet(self):
        """Sans second facteur enregistré, ``login`` délivre les jetons.

        C'est le comportement complet du noyau open source, pas un mode dégradé.
        """
        from th2agent.core.extensions.registry import ExtensionRegistry

        assert ExtensionRegistry().second_factor() is None

    def test_les_endpoints_d_auth_du_noyau_sont_toujours_la(self):
        from th2agent.auth.router import router

        chemins = {r.path for r in router.routes}
        for attendu in ("/auth/token", "/auth/logout", "/auth/forgot-password",
                        "/auth/reset-password", "/auth/verify-email"):
            assert attendu in chemins, attendu

    def test_les_endpoints_commerciaux_ont_disparu_du_noyau(self):
        from th2agent.auth.router import router as routeur_auth
        from th2agent.users.router import router as routeur_users

        chemins = {r.path for r in routeur_auth.routes} | {r.path for r in routeur_users.routes}
        commerciaux = {c for c in chemins if "/mfa/" in c or c.split("/")[-1] in
                       {"github", "google", "microsoft", "linkedin"}}
        assert not commerciaux, f"routes commerciales encore dans le noyau : {commerciaux}"

    def test_les_integrations_restent_dans_le_noyau(self):
        """Le piège : OAuth sert aussi à brancher Drive/Gmail/Outlook comme
        outils d'agent. Le tableau d'offres les garde en open source."""
        for module in ("th2agent.integrations.google", "th2agent.integrations.microsoft"):
            assert importlib.import_module(module) is not None
