"""Templates are bilingual: the English catalog entry and the English build carry no French.

Only the text a reader sees is checked (titles, labels, notes, units, and the string
literals of SQL expressions, which become category values). Source names, derived
column names and filter ids are identifiers and may stay French.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from apowerb.bi.spec import catalog
from tests.helpers.bi_template_cases import CASES, TEMPLATE_IDS, build_case

TEXT_KEYS = {"label", "title", "description", "subtitle", "text", "secondary", "unit", "target_label"}
SQL_KEYS = {"expr", "query", "where"}
FRENCH = re.compile(
    r"[éèàçêëîïôûùœ«»]"
    r"|\b(de|des|du|la|le|les|et|ou|par|sur|pour|avec|sans|dont|aux?|une?|sem|mois|jours?|semaines?"
    r"|toutes?|tous|ventes?|stocks? de|taux|retards?|livr\w*|commandes?|fournisseurs?|familles?"
    r"|chiffre|factures?|tr[eé]sorerie|effectifs?|chantiers?|marges?|prévu\w*|réel\w*)\b",
    re.IGNORECASE,
)
OPTIONAL_PLURAL = re.compile(r"\w\((s|es|ies|x)\)")  # "famille(s)", "product family(ies)"
_LITERAL = re.compile(r"'((?:[^']|'')*)'")
# {{measure|format}} tokens and `column` names are identifiers; the words of {{measure|plural:one:other}} are text
_TOKEN = re.compile(r"\{\{[^}|]*(?:\|\s*plural:([^}]*))?[^}]*\}\}|`[^`]*`")


def _visible_texts(node: Any, key: str | None = None) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            found += _visible_texts(v, k)
    elif isinstance(node, list):
        for v in node:
            found += _visible_texts(v, key)
    elif isinstance(node, str):
        if key in TEXT_KEYS:
            found.append((key, node))
        elif key in SQL_KEYS:
            found += [(f"{key} literal", lit) for lit in _LITERAL.findall(node)]
    return found


def _words(text: str) -> str:
    return _TOKEN.sub(lambda m: (m.group(1) or "").replace(":", " "), text)


def _french(texts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(k, t) for k, t in texts if FRENCH.search(_words(t))]


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_english_build_has_no_french_visible_text(template_id: str, case: str) -> None:
    spec = build_case(template_id, case, lang="en")
    assert _french(_visible_texts(spec)) == []


@pytest.mark.parametrize("lang", ["fr", "en"])
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_build_has_no_optional_plural_marks(template_id: str, case: str, lang: str) -> None:
    spec = build_case(template_id, case, lang=lang)
    assert [(k, t) for k, t in _visible_texts(spec) if OPTIONAL_PLURAL.search(_words(t))] == []


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_english_build_differs_from_the_french_one(template_id: str) -> None:
    assert build_case(template_id, "full", lang="en") != build_case(template_id, "full", lang="fr")


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_unknown_language_is_rejected(template_id: str) -> None:
    with pytest.raises(ValueError):
        build_case(template_id, "required", lang="de")


def test_catalog_lists_every_template_module() -> None:
    assert [t.id for t in catalog.list_templates()] == TEMPLATE_IDS


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_english_catalog_entry_has_no_french(template_id: str) -> None:
    template = catalog.get_template(template_id, lang="en")
    assert template is not None
    described = template.describe()
    texts = [("label", described["label"]), ("description", described["description"])]
    texts += [("column", c["label"]) for c in described["columns"]]
    texts += [("param", p["label"]) for p in described["params"]]
    assert _french(texts) == []


@pytest.mark.parametrize("template_id", TEMPLATE_IDS)
def test_french_catalog_entry_is_the_default(template_id: str) -> None:
    assert catalog.get_template(template_id).describe() == catalog.get_template(template_id, lang="fr").describe()
    assert catalog.get_template(template_id, lang="en").describe()["label"] != catalog.get_template(template_id).describe()["label"]


def test_catalog_entry_builds_in_its_language() -> None:
    template = catalog.get_template("delivery_otif", lang="en")
    columns, _ = template.clean_columns({})
    spec = template.build("11111111-2222-3333-4444-555555555555", {**columns, **{
        "order_date": "date_commande", "promised_date": "date_promise", "delivered_date": "date_livraison",
        "qty_ordered": "qte_commandee", "qty_delivered": "qte_livree", "customer": "client",
    }}, {})
    assert _french(_visible_texts(spec)) == []


def test_clean_columns_messages_follow_the_language() -> None:
    _, errors = catalog.get_template("delivery_otif", lang="en").clean_columns({"nope": "x"})
    assert errors and all(not FRENCH.search(e) for e in errors)
    _, errors_fr = catalog.get_template("delivery_otif", lang="fr").clean_columns({"nope": "x"})
    assert any("obligatoire" in e for e in errors_fr)
