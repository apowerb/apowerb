"""Un signalement ne doit jamais transporter de secret.

L'endpoint demande à l'utilisateur ce qu'il avait à l'écran : l'URL en
cours, ses derniers appels d'API, ses erreurs de console. Les trois
transportent des jetons. Ces tests éprouvent la seule chose qui empêche
un secret d'atterrir dans une issue.

Le mode d'échec visé est vécu (04/09/2026) : un filtre par *préfixe* sur
l'environnement d'un service a sorti un mot de passe ERP en clair. D'où
les deux assertions structurantes ci-dessous — la valeur part, le nom
reste.

Les jetons de ces tests sont volontairement écrits en `xxxx` : ils ont la
FORME que le filtre reconnaît, et aucun ne ressemble à une valeur réelle
qu'un lecteur pressé pourrait prendre pour un secret égaré.
"""

import pytest

from apowerb.bug_reports.redaction import (
    REDACTED,
    redact_mapping,
    redact_text,
    redact_url,
)


@pytest.mark.parametrize(
    "faux_jeton",
    [
        "ghp_xxxxxxxxxxxxxxxxxxxxxxxx",
        "sk-xxxxxxxxxxxxxxxxxxxxxxxx",
        "AKIAXXXXXXXXXXXXXXXX",
        "eyJhbGciOiJIUzIxxx.eyJzdWIiOiJ4eHgifQ.xxxxxxxxxxxxxxxxxxxx",
    ],
)
def test_les_formes_de_jetons_connues_partent_meme_sans_cle(faux_jeton):
    """Un jeton nu dans un message d'erreur n'a pas de `clé=` devant lui."""
    assert faux_jeton not in redact_text(
        f"la requête a échoué avec {faux_jeton} en en-tête"
    )


def test_bearer_part_meme_quand_le_jeton_na_aucune_forme_connue():
    """Le porteur est reconnu par son mot-clé, pas par la tête du jeton."""
    assert "opaque-token-value" not in redact_text(
        "Authorization: Bearer opaque-token-value-1234"
    )


def test_la_valeur_part_mais_le_nom_reste():
    """Masquer, pas supprimer.

    Voir `password=<redacted>` dit au relecteur qu'un mot de passe
    circulait là ; effacer la ligne entière le lui cache, et il cherchera
    ailleurs.
    """
    out = redact_text("connexion refusée password=xxxxxx pour user=alice")
    assert "password=" in out
    assert REDACTED in out
    # Ce qui n'est pas sensible survit : sans ça, le rapport perd son sens.
    assert "user=alice" in out


def test_un_nom_sensible_hors_prefixe_est_couvert():
    """`pwd` n'a pas le préfixe de `password` — la liste blanche l'attrape."""
    out = redact_text("pwd=xxxxxx")
    assert out == f"pwd={REDACTED}"


def test_url_le_chemin_reste_la_valeur_sensible_part():
    """Le chemin dit OÙ le bug s'est produit : c'est la première chose à lire."""
    out = redact_url("https://app.io/oauth/callback?code=xxxxxx&state=xyz&page=2")
    assert out.startswith("https://app.io/oauth/callback?")
    assert "code=xxxxxx" not in out
    assert "state=xyz" in out and "page=2" in out


def test_url_sans_query_est_rendue_telle_quelle():
    assert redact_url("/api/agents/42") == "/api/agents/42"


def test_mapping_recursif_et_borne():
    data = {
        "headers": {"Authorization": "Bearer xxxxxxxxxxxxxxxx", "Accept": "json"},
        "items": [{"api_key": "xxxxxx"}, {"label": "ok"}],
    }
    out = redact_mapping(data)
    assert out["headers"]["Authorization"] == REDACTED
    assert out["headers"]["Accept"] == "json"
    assert out["items"][0]["api_key"] == REDACTED
    assert out["items"][1]["label"] == "ok"


def test_le_texte_est_borne():
    """Une trace de 2 Mo ne doit pas devenir une ligne de table."""
    out = redact_text("a" * 10_000, max_length=100)
    assert len(out) < 200
    assert "tronqué" in out
