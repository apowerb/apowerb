"""LiteLlm model construction and output-schema instruction helpers.

Extracted from ``agent_utils`` to keep the main builder below the 500-line
threshold.
"""
from __future__ import annotations

import json
import os

from google.adk.models.lite_llm import LiteLlm

from th2agent.configs.th2logger import setup_logging
from th2agent.core.agent_helpers.default_llm import (
    DEFAULT_LLM_MODEL_ID,
    is_default_llm_model,
    resolve_model_credentials,
)
from th2agent.helpers.encryptor import decrypt_value_in_dict


logger = setup_logging(__name__)


def build_litellm_model(agent_details: dict, temperature: float | None) -> LiteLlm:
    """Construct a ``LiteLlm`` instance from the agent's model config.

    Handles the thaink2 default model, OVH Mistral defaults, OpenAI-compat
    api_base overrides, and Gemini API key env handling.
    """
    _litellm_kwargs: dict = {}
    if temperature is not None:
        _litellm_kwargs["temperature"] = temperature

    agent_model_params = agent_details.get("agent_model_params") or {}
    if isinstance(agent_model_params, str):
        agent_model_params = json.loads(agent_model_params)

    agent_model_params = decrypt_value_in_dict(
        agent_model_params, values_to_decrypt=["model_api_key"]
    )

    agent_model = agent_details["agent_model"]
    if is_default_llm_model(agent_model):
        # Modele mutualise : les credentials viennent de l'environnement du
        # serveur, jamais de l'agent. `resolve_model_credentials` ecrase donc
        # cle et api_base -- un agent ne peut pas detourner le trafic.
        agent_model, agent_model_params = resolve_model_credentials(
            agent_model, agent_model_params
        )
        agent_details = {**agent_details, "agent_model": agent_model}
        logger.info(f"[TO_AGENT] Using thaink2 default model -> {agent_model}")

    model_api_base = agent_model_params.get("model_api_base")
    model_api_key = agent_model_params.get("model_api_key")

    logger.info(
        f"[TO_AGENT] model_api_base={'set' if model_api_base else 'not set'}, model_api_key={'set' if model_api_key else 'not set'}"
    )

    # Default OVH Mistral endpoint
    if not model_api_base and agent_details["agent_model"].startswith("mistral/"):
        model_api_base = "https://mistral-small-3-2-24b-instruct-2506.endpoints.kepler.ai.cloud.ovh.net/api/openai_compat/v1"
        logger.info(
            f"[TO_AGENT] Using default OVH Mistral endpoint: {model_api_base}"
        )

    if model_api_base:
        _model_name = agent_details["agent_model"]
        # When using a custom OpenAI-compat api_base, force the openai/ provider prefix
        # so litellm uses the OpenAI tool-calling path instead of the Mistral SDK path.
        if not _model_name.startswith("openai/"):
            _model_name = "openai/" + _model_name.split("/", 1)[-1]
        return LiteLlm(
            model=_model_name,
            api_base=model_api_base,
            api_key=model_api_key,
            drop_params=True,
            **_litellm_kwargs,
        )

    # For Gemini models, LiteLLM reads API key from env var, not api_key param
    if model_api_key and agent_details["agent_model"].startswith("gemini/"):
        os.environ["GEMINI_API_KEY"] = model_api_key
    elif model_api_key:
        _litellm_kwargs["api_key"] = model_api_key
    # Thinking Gemini REACTIVE (22/07/26) : la casse d'appariement __thought__
    # (incident 03/06, PR #196, thinking={"type":"disabled"}) etait une
    # regression google-adk 1.26.0 (#4650), corrigee >=1.27 : la signature
    # voyage desormais dans Part.thought_signature (round-trip complet).
    # reasoning_effort="low" borne le cout ; les strips de litellm_config
    # restent en filet anti-orphelins. Kill-switch sans redeploiement :
    # GEMINI_REASONING_EFFORT=none (efficace sur gemini-2.5.x uniquement).
    if agent_details["agent_model"].startswith("gemini/"):
        _litellm_kwargs["reasoning_effort"] = os.environ.get(
            "GEMINI_REASONING_EFFORT", "low"
        )
    return LiteLlm(model=agent_details["agent_model"], **_litellm_kwargs)


def validate_agent_model(agent_model: str, agent_model_params=None, agent_type: str | None = None) -> None:
    """Fail-fast a la creation/MAJ d'un agent : rejette un ``agent_model`` dont
    litellm ne peut PAS deduire le provider.

    Sans cette garde, un modele comme ``ovh/Qwen3.5`` (provider ``ovh`` inexistant)
    ou ``string`` est accepte en base puis leve ``litellm.BadRequestError`` "LLM
    Provider NOT provided" a CHAQUE tour -> agent muet/inutilisable, sans message
    clair (bug live 09/06, agent cree par un collegue). On valide donc le provider
    a l'ecriture, ou l'utilisateur peut corriger.

    NO-OP si un ``model_api_base`` est fourni : le builder force alors le prefixe
    ``openai/`` (endpoint OpenAI-compat), toujours resolvable. ``mistral/`` et
    ``gemini/`` passent (providers litellm connus) ; les modeles bare connus aussi
    (``gpt-4o-mini`` -> openai). Leve ``ValueError`` (message actionnable) sinon.
    """
    # Agents conteneurs (sequential/parallel/loop) : ADK n'utilise PAS leur modele
    # a l'execution (orchestration pure) -> un modele vide y est legitime, ne pas valider.
    if (agent_type or "").strip().lower() in {"sequential", "parallel", "loop"}:
        return
    # Modele thaink2 mutualise : litellm ne connait pas le provider `thaink2`
    # (c'est une sentinelle resolue au runtime), on valide donc que le serveur
    # le propose vraiment -- sinon l'agent serait cree puis muet a chaque tour,
    # exactement le bug que cette fonction existe pour empecher.
    if is_default_llm_model(agent_model):
        from th2agent.core.agent_helpers.default_llm import default_llm_available

        if not default_llm_available():
            raise ValueError(
                f"Le modele '{DEFAULT_LLM_MODEL_ID}' n'est pas disponible sur ce "
                "serveur : DEFAULT_LLM_MODEL / DEFAULT_LLM_API_KEY ne sont pas "
                "configures. Choisir un modele et fournir une cle API."
            )
        return
    params = agent_model_params or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (ValueError, TypeError):
            params = {}
    if isinstance(params, dict) and params.get("model_api_base"):
        return  # endpoint custom OpenAI-compat -> valide par construction
    model = (agent_model or "").strip()
    try:
        from litellm import get_llm_provider

        get_llm_provider(model)
    except Exception as exc:  # noqa: BLE001 - on retraduit en erreur metier claire
        raise ValueError(
            f"Modele '{model or '(vide)'}' invalide : litellm ne reconnait pas le "
            "provider. Prefixe le modele par un provider connu (ex "
            "'gemini/gemini-2.5-flash', "
            "'mistral/Mistral-Small-3.2-24B-Instruct-2506', 'anthropic/claude-...', "
            "'openai/...'), ou renseigne un model_api_base pour un endpoint "
            "OpenAI-compat."
        ) from exc


def apply_output_schema(instruction: str, output_schema, agent_name: str) -> str:
    """Append the output-format instruction from ``output_schema`` if valid."""
    if not output_schema:
        return instruction
    parsed = (
        json.loads(output_schema)
        if isinstance(output_schema, str)
        else output_schema
    ) or {}
    output_instruction = parsed.get("instruction", "")
    if (
        isinstance(output_instruction, str)
        and 0 < len(output_instruction) <= 2000
    ):
        instruction += f"\n\n## Output Format\n{output_instruction}"
        logger.info(
            f"[TO_AGENT] Output schema instruction injected for {agent_name}"
        )
    elif output_instruction:
        logger.warning(
            f"[TO_AGENT] output_schema instruction too long or invalid for {agent_name}, skipping"
        )
    return instruction
