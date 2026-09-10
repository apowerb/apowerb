"""Le corps d'issue doit se lire, et ne pas publier ce qui ne doit pas l'être.

Deux exigences distinctes :

- **lisibilité** — celui qui ouvre le ticket doit savoir en dix lignes
  s'il sait corriger : observé, attendu, où l'utilisateur était, comment
  reproduire, puis la preuve ;
- **discrétion** — la capture d'écran d'un utilisateur n'est pas publiée
  dans le corps, seulement signalée avec un lien vers l'écran de triage.
"""

from apowerb.bug_reports.issue_body import render_issue_body, render_issue_title

RAPPORT = {
    "id": 12,
    "title": "Le bouton Exécuter reste gris",
    "area": "tools",
    "severity": "blocker",
    "occurrences": 3,
    "fingerprint": "ab12cd34ef567890",
    "route": "/agents/{id}/tools",
    "reporter_email": "temoin@example.org",
    "where_i_was": "Configuration de l'agent commercial, onglet Outils",
    "what_i_did": "1. Ouvrir l'agent\n2. Onglet Outils\n3. Cliquer Exécuter",
    "expected": "Le test se lance",
    "observed": "Rien ne se passe",
    "context": {
        "screen": "Configuration agent",
        "section": "Outils",
        "app_version": "0.1.22",
        "last_action": {"label": "Exécuter", "kind": "clic"},
        "navigation_trail": [
            {"label": "Agents", "route": "/agents", "dwell_ms": 12000},
            {"label": "Configuration agent", "route": "/agents/42/tools", "dwell_ms": 95000},
        ],
    },
    "server_logs": [
        {
            "timestamp": "2026-09-10T11:00:00Z",
            "level": "ERROR",
            "logger": "apowerb.tools",
            "message": "tool config invalid",
        }
    ],
    "request_ids": ["abc123"],
    "api_calls": [
        {"method": "post", "path": "/api/tools/run", "status": 500, "request_id": "abc123"}
    ],
    "has_screenshot": True,
}


def test_le_titre_situe_le_defaut_par_sa_route():
    assert render_issue_title(RAPPORT) == "[/agents/{id}/tools] Le bouton Exécuter reste gris"


def test_un_titre_absent_est_deduit_de_lobserve():
    titre = render_issue_title({"route": "/x", "observed": "Rien ne se passe"})
    assert "Rien ne se passe" in titre


def test_les_sections_essentielles_sont_presentes():
    corps = render_issue_body(RAPPORT)
    for attendu in [
        "**Observé**",
        "**Attendu**",
        "## Où j'étais",
        "## Reproduction",
        "## Logs serveur",
        "Outils",  # libellé de la zone, pas son identifiant seul
    ]:
        assert attendu in corps, f"section manquante : {attendu}"


def test_le_fil_de_navigation_est_rendu_dans_lordre():
    corps = render_issue_body(RAPPORT)
    assert "1. Agents" in corps
    assert corps.index("1. Agents") < corps.index("2. Configuration agent")


def test_la_capture_nest_pas_publiee_seulement_signalee():
    """Elle montre l'écran d'un utilisateur : le lien reste interne."""
    corps = render_issue_body(RAPPORT, app_url="https://app.example.com")
    assert "https://app.example.com/admin/bug-reports/12" in corps
    assert "data:image" not in corps
    assert "base64" not in corps


def test_labsence_de_logs_est_dite_avec_les_identifiants_a_chercher():
    """Le silence serait pris pour « il n'y a rien eu ». Il faut le remède."""
    sans_logs = {**RAPPORT, "server_logs": []}
    corps = render_issue_body(sans_logs)
    assert "Aucune ligne retrouvée" in corps
    assert "abc123" in corps


def test_le_compte_des_signalements_apparait_quand_il_y_en_a_plusieurs():
    assert "3 (même empreinte" in render_issue_body(RAPPORT)
    seul = render_issue_body({**RAPPORT, "occurrences": 1})
    assert "même empreinte" not in seul


def test_un_rapport_minimal_ne_casse_pas_le_rendu():
    corps = render_issue_body({"id": 1, "fingerprint": "x", "what_i_did": "rien"})
    assert "## Reproduction" in corps
    assert "Signalement #1" in corps
