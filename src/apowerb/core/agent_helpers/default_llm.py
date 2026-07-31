"""Modele « thaink2 par defaut » : credentials portees par l'environnement
du serveur, jamais par l'agent.

Un agent dont ``agent_model`` vaut ``thaink2/default`` ne stocke ni cle ni
endpoint : le triplet (modele, cle, api_base) est resolu au runtime depuis
les settings (``DEFAULT_LLM_MODEL`` / ``DEFAULT_LLM_API_KEY`` /
``DEFAULT_LLM_API_BASE``). L'utilisateur n'a donc « ni son API, ni sa cle
API, ni son modele » a saisir (demande Farid, 27/07/26).

La consequence recherchee est de securite : la cle mutualisee ne transite
ni par la base ni par ``GET /agents/{id}``, donc elle n'est pas lisible
depuis le navigateur. Elle est aussi le pivot de la facturation a venir --
tout ce qui passe par ce modele est a la charge de thaink2, d'ou l'entree
d'indirection : basculer Mistral -> Gemini se fait cote env, sans toucher
aux agents existants.

Le masquage (``mask_model_api_key`` / ``unmask_model_api_key``) couvre le
cas general : l'API ne renvoyait aucune cle chiffree mais bien la valeur
en clair (« for frontend convenience »), y compris celles des utilisateurs.
"""
from __future__ import annotations

import json

from apowerb.configs.settings import get_settings


# Identifiant sentinelle stocke dans ``agent_model``. Un seul champ porte
# l'information (pas de flag parallele dans agent_model_params) : impossible
# de desynchroniser « quel modele » et « quelles credentials ».
DEFAULT_LLM_PROVIDER = "thaink2"
DEFAULT_LLM_MODEL_ID = f"{DEFAULT_LLM_PROVIDER}/default"

# Renvoye a la place d'une cle enregistree. Le front l'affiche comme un
# champ rempli non modifiable ; s'il nous le renvoie, on recolle la vraie
# valeur (cf ``unmask_model_api_key``).
MASKED_API_KEY = "__unchanged__"

_SECRET_PARAM_KEYS = ("model_api_key", "model_api_base")


def _as_dict(params) -> dict:
    """``agent_model_params`` arrive tantot en dict, tantot en JSON string."""
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (ValueError, TypeError):
            return {}
    return dict(params) if isinstance(params, dict) else {}


def is_default_llm_model(agent_model: str | None) -> bool:
    return (agent_model or "").strip() == DEFAULT_LLM_MODEL_ID


def default_llm_available() -> bool:
    """Le modele par defaut n'est proposable que si l'env le fournit."""
    settings = get_settings()
    return bool(
        (getattr(settings, "default_llm_model", "") or "").strip()
        and (getattr(settings, "default_llm_api_key", "") or "").strip()
    )


def resolve_model_credentials(agent_model: str | None, model_params) -> tuple[str, dict]:
    """(modele, params) effectifs a utiliser pour un appel LLM.

    Passe-plat pour un agent normal. Pour ``thaink2/default``, retourne les
    credentials de l'environnement et **ignore integralement** ce que porte
    l'agent : une cle ou un ``model_api_base`` plantes en base (agent seede,
    import, ecriture directe) ne doivent pas pouvoir detourner le trafic ni
    faire payer thaink2 pour un endpoint tiers.
    """
    params = _as_dict(model_params)
    if not is_default_llm_model(agent_model):
        return (agent_model or ""), params

    settings = get_settings()
    model = (getattr(settings, "default_llm_model", "") or "").strip()
    api_key = (getattr(settings, "default_llm_api_key", "") or "").strip()
    if not model or not api_key:
        raise ValueError(
            "Le modele thaink2 par defaut n'est pas configure sur ce serveur : "
            "renseigner DEFAULT_LLM_MODEL et DEFAULT_LLM_API_KEY."
        )

    resolved = {k: v for k, v in params.items() if k not in _SECRET_PARAM_KEYS}
    resolved["model_api_key"] = api_key
    api_base = (getattr(settings, "default_llm_api_base", "") or "").strip()
    if api_base:
        resolved["model_api_base"] = api_base
    return model, resolved


def strip_default_llm_params(agent_model: str | None, model_params) -> dict:
    """Params a persister : pour un agent « default », ni cle ni endpoint."""
    params = _as_dict(model_params)
    if not is_default_llm_model(agent_model):
        return params
    return {k: v for k, v in params.items() if k not in _SECRET_PARAM_KEYS}


def mask_model_api_key(model_params) -> dict:
    """Remplace une cle enregistree par le sentinelle avant de sortir de l'API.

    Un champ vide reste vide : le front doit pouvoir distinguer « aucune cle »
    de « cle enregistree, non revelee ».
    """
    params = _as_dict(model_params)
    if params.get("model_api_key"):
        params["model_api_key"] = MASKED_API_KEY
    return params


def unmask_model_api_key(incoming_params, stored_params) -> dict:
    """Recolle la cle stockee quand le client nous renvoie le sentinelle.

    Sans ce recollage, sauvegarder un agent sans toucher au champ cle
    (renommage, changement d'instruction) ecraserait la cle par le masque --
    meme classe de bug que le PUT qui vidait les champs pipeline.
    """
    params = _as_dict(incoming_params)
    if params.get("model_api_key") != MASKED_API_KEY:
        return params
    stored = _as_dict(stored_params)
    params["model_api_key"] = stored.get("model_api_key") or ""
    return params
