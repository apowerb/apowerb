"""Aggregate all SuperAgent templates, preserving the original ordering."""

from apowerb.core.superagents.templates.rag import RAG_TEMPLATES
from apowerb.core.superagents.templates.data import DATA_TEMPLATES
from apowerb.core.superagents.templates.dashboard import DASHBOARD_TEMPLATES
from apowerb.core.superagents.templates.marketing import MARKETING_TEMPLATES
from apowerb.core.superagents.templates.image import IMAGE_TEMPLATES
from apowerb.core.superagents.templates.audio import AUDIO_TEMPLATES
from apowerb.core.extensions.registry import registry as _ext_registry


# Preserve the original ordering from the monolithic superagents.py file:
# rag_agent, text_to_sql_agent, image_analyst, email_marketing_agent,
# data_analyst_agent, forecasting_agent, knowledge_assistant, dashboard_agent,
# image_creator, audio_transcriber, audio_assistant
def _build_templates() -> list[dict]:
    by_id: dict[str, dict] = {}
    for group in (
        RAG_TEMPLATES,
        DATA_TEMPLATES,
        DASHBOARD_TEMPLATES,
        MARKETING_TEMPLATES,
        IMAGE_TEMPLATES,
        AUDIO_TEMPLATES,
    ):
        for tpl in group:
            by_id[tpl["template_id"]] = tpl

    # Templates apportes par les briques et overlays, via le registre.
    # Le noyau ne nomme aucun template commercial : la prospection arrive par
    # ce chemin exactement comme un overlay client.
    for tpl in _ext_registry.templates():
        by_id.setdefault(tpl["template_id"], tpl)

    ordered_ids = [
        "rag_agent",
        "text_to_sql_agent",
        "database_assistant",
        "image_analyst",
        "email_marketing_agent",
        # data_analyst_agent intentionally omitted: it duplicated display_name
        # "Data Analyst", now owned by text_to_sql_agent. Kept in DATA_TEMPLATES
        # for backward-compat with agents already created from it.
        "forecasting_agent",
        "knowledge_assistant",
        "dashboard_agent",
        "image_creator",
        "audio_transcriber",
        "audio_assistant",
    ]
    ordered = [by_id[t] for t in ordered_ids if t in by_id]
    # Overlay-provided templates (e.g. a client overlay) are appended in registration
    # order — the core never hardcodes client template ids.
    _placed = set(ordered_ids)
    for _tpl in _ext_registry.templates():
        if _tpl["template_id"] not in _placed:
            ordered.append(by_id[_tpl["template_id"]])
            _placed.add(_tpl["template_id"])
    # Two visible templates must never share a display_name (the picker would
    # show two identical entries). Fail loudly if a future edit reintroduces one.
    seen: dict[str, str] = {}
    for tpl in ordered:
        dn = tpl["display_name"]
        if dn in seen:
            raise ValueError(
                f"Duplicate superagent display_name {dn!r}: "
                f"{seen[dn]} and {tpl['template_id']}"
            )
        seen[dn] = tpl["template_id"]
    return ordered


SUPERAGENT_TEMPLATES: list[dict] = _build_templates()

__all__ = ["SUPERAGENT_TEMPLATES"]
