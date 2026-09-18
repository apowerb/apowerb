"""Ce que les outils de carte d'action renvoient au modèle (roadmap#79).

Le modèle voit le retour d'un outil. Quand ce retour était la carte complète
— question, choix, marqueur ``_action_card`` —, Gemini la recopiait dans sa
réponse, et le JSON apparaissait dans le chat, dans l'export ``.md`` et sur
les instantanés publics ``/share``.

Le front n'en a pas besoin : il construit la carte à partir des ARGUMENTS de
l'appel d'outil (``useChat.js``, ``onToolCall`` → ``data: {...toolCall.args}``),
dans le front commercial comme dans le front OSS. Le retour n'est donc qu'un
accusé de réception pour le modèle, et il ne doit rien contenir qu'il puisse
recopier.
"""

import json

import pytest

from apowerb.core.agent_helpers import chat_action_tools as t

# Tout ce qui vient de l'utilisateur ou de la carte porte ce marqueur : s'il
# apparait dans le retour, c'est que le contenu de la carte est renvoye au modele.
M = "SENTINELLE"

CAS = [
    ("request_user_input", "user_input",
     dict(question=f"{M} question", input_type="chips", choices=[f"{M} a", f"{M} b"], placeholder=f"{M} indice")),
    ("confirm_destructive", "confirm_destructive",
     dict(action=f"{M} action", impact=f"{M} impact", item=f"{M} item")),
    ("request_payment", "payment",
     dict(amount=42.0, currency="EUR", reason=f"{M} raison", checkout_url=f"https://x/{M}")),
    ("schedule_followup", "followup",
     dict(when_iso="2026-09-20T10:00:00", recap=f"{M} recap", calendar_link=f"https://x/{M}")),
    ("propose_artifact_edit", "artifact_edit",
     dict(filename=f"{M}.md", diff=f"{M} diff", summary=f"{M} resume")),
    ("request_file_from_user", "file_request",
     dict(purpose=f"{M} but", accept=".pdf", max_size_mb=5)),
    ("propose_agent_upgrade", "agent_upgrade",
     dict(capability=f"{M} capacite", reason=f"{M} raison", skill_id=f"{M}-skill", tool_name=f"{M}_tool")),
    ("request_location", "location_request",
     dict(reason=f"{M} raison", precision="coarse")),
]


@pytest.mark.parametrize("nom,kind,kwargs", CAS, ids=[c[0] for c in CAS])
class TestAccuseDeReception:
    def test_le_retour_ne_contient_rien_de_la_carte(self, nom, kind, kwargs):
        r = getattr(t, nom)(**kwargs)
        assert M not in json.dumps(r, ensure_ascii=False), (
            f"{nom} renvoie encore le contenu de la carte au modele : {r}"
        )

    def test_le_retour_ne_porte_plus_le_marqueur_de_carte(self, nom, kind, kwargs):
        # C'est ce marqueur que le front (#248) doit aujourd'hui filtrer dans
        # le texte recopie : s'il n'est plus dans le retour, rien a recopier.
        assert "_action_card" not in getattr(t, nom)(**kwargs)

    def test_le_retour_dit_au_modele_que_la_carte_est_affichee(self, nom, kind, kwargs):
        r = getattr(t, nom)(**kwargs)
        assert r["status"] == "displayed"
        assert r["kind"] == kind
        assert "do not repeat" in r["note"].lower()


def test_une_erreur_de_saisie_reste_explicite_pour_le_modele():
    # Le modele doit toujours savoir pourquoi l'outil a refuse, pour corriger.
    r = t.request_user_input(question="Q", input_type="inexistant")
    assert r["status"] == "error"
    assert "inexistant" in r["message"]
