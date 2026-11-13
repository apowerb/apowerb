"""Regles propres aux generations Gemini, partagees par le builder et le
wrapper litellm.

Deprecation Google (mail 04/11/2026) : le 01/12/2026 ``gemini-2.5-flash`` est
redirige d'office vers ``gemini-3.5-flash-lite``. On fait le renommage de notre
cote pour que litellm construise une requete Gemini 3 (thinkingLevel au lieu de
thinkingBudget) et que la grille tarifaire trouve la bonne ligne. L'alias est
toujours applique : ``gemini-2.5-flash`` est arrete, les agents et configs
enregistres sous l'ancien nom continuent donc de fonctionner.

A partir de Gemini 3.5 Flash-Lite / 3.6 Flash, ``temperature``, ``top_p`` et
``top_k`` sont ignores puis refuses (400) ; ``candidate_count`` n'existe plus.
"""
from typing import Any, Dict

_DEPRECATED_ALIASES = {
    "gemini-2.5-flash": "gemini-3.5-flash-lite",
}
_GEMINI3_DROPPED_PARAMS = ("temperature", "top_p", "top_k", "candidate_count")


def effective_gemini_model(model: str) -> str:
    """Nom reellement envoye : alias des modeles Gemini deprecies (match exact,
    prefixe ``gemini/`` conserve). Tout autre modele est rendu tel quel."""
    if not isinstance(model, str):
        return model
    prefix, _, name = model.rpartition("/")
    target = _DEPRECATED_ALIASES.get(name)
    if target is None or prefix not in ("", "gemini"):
        return model
    return f"{prefix}/{target}" if prefix else target


def is_gemini_3_or_newer(model: str) -> bool:
    """Meme test que litellm (``"gemini-3" in model``), applique au nom effectif."""
    return isinstance(model, str) and "gemini-3" in effective_gemini_model(model)


def drop_gemini3_sampling_params(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Applique l'alias a ``kwargs["model"]`` et retire les parametres
    d'echantillonnage que Gemini 3.x ignore ou refuse. NO-OP pour les autres
    modeles ; ne leve jamais (tourne sur chaque completion)."""
    model = kwargs.get("model")
    if not isinstance(model, str):
        return kwargs
    kwargs["model"] = effective_gemini_model(model)
    if is_gemini_3_or_newer(kwargs["model"]):
        for param in _GEMINI3_DROPPED_PARAMS:
            kwargs.pop(param, None)
    return kwargs
