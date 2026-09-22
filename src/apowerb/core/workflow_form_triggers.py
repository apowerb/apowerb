"""``form`` (T2) — formulaire public/authentifié qui lance un run.

Réutilise TEL QUEL le jeton haché/chiffré du contrat webhook (T1,
``workflow_triggers.sync_trigger_for_workflow`` : ``token_hash``/
``token_encrypted``, même rotation, même 404 opaque) — un formulaire n'est
qu'un DEUXIÈME kind qui porte un jeton, pas un mécanisme séparé.

Deux responsabilités, séparées pour rester testables sans HTTP :

* ``find_active_form_trigger`` — le jeton -> la ligne trigger (kind
  ``form``, actif), symétrique de
  ``workflow_triggers.find_active_webhook_trigger``.
* ``validate_form_values`` — validation SERVEUR des valeurs soumises contre
  ``fields`` (types, requis, options d'un ``select``) ; ne fait AUCUNE
  confiance à ce que le client a pu valider côté navigateur. Ne renvoie que
  les champs déclarés — une clé non déclarée dans la soumission est droppée,
  jamais transmise telle quelle au run.
"""

from __future__ import annotations

import hmac
from typing import Any, Optional

from apowerb.core import workflow_triggers as wt


def find_active_form_trigger(token: str) -> Optional[dict]:
    """La ligne dont le hash du jeton correspond, kind ``form`` et armée,
    sinon ``None`` — un jeton inconnu et un workflow dépublié rendent le même
    404 opaque, comme pour le webhook (T1)."""
    token_hash = wt.hash_token(token)
    t = wt.workflow_trigger_store.trigger_table
    with wt.workflow_trigger_store.engine.begin() as conn:
        row = conn.execute(
            t.select().where(t.c.kind == "form", t.c.token_hash == token_hash)
        ).fetchone()
    if row is None:
        return None
    d = dict(row._mapping)
    if not hmac.compare_digest(d["token_hash"], token_hash):
        return None
    if not d.get("active"):
        return None
    return d


def _validate_one(field: dict, value: Any) -> Optional[str]:
    ftype = field.get("type")
    name = field["name"]
    if ftype in ("text", "textarea", "date"):
        if not isinstance(value, str):
            return f"{name} doit être une chaîne"
    elif ftype == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{name} doit être un nombre"
    elif ftype == "boolean":
        if not isinstance(value, bool):
            return f"{name} doit être un booléen"
    elif ftype == "select":
        options = field.get("options") or []
        if value not in options:
            return f"{name} doit être l'une de : {', '.join(map(str, options))}"
    return None


def validate_form_values(
    fields: list[dict], values: dict
) -> tuple[bool, Optional[str], dict]:
    """``(valide, message d'erreur, valeurs nettoyées)``.

    Les valeurs nettoyées ne contiennent QUE les champs déclarés par
    ``fields`` — une clé de la soumission absente du schéma est ignorée,
    jamais transmise au payload du run.
    """
    cleaned: dict = {}
    for field in fields:
        name = field["name"]
        present = name in values
        if not present:
            if field.get("required", True):
                return False, f"{name} est requis", {}
            continue
        value = values[name]
        error = _validate_one(field, value)
        if error is not None:
            return False, error, {}
        cleaned[name] = value
    return True, None, cleaned
