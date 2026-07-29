"""Garde-fous sur le template SuperAgent text_to_sql_agent.

Le template route les charts selon 3 modes :
- INLINE dans le chat -> tool_create_chart + embed_chart (rendu recharts) ;
- FICHIER téléchargeable (HTML) -> tool_visualize_data (Plotly) ;
- export DONNÉES (CSV) -> create_downloadable_file.
"""

from __future__ import annotations


def _template():
    from th2agent.core.superagents.templates.data import DATA_TEMPLATES

    for t in DATA_TEMPLATES:
        if t["template_id"] == "text_to_sql_agent":
            return t
    raise AssertionError("template text_to_sql_agent introuvable")


class TestTextToSqlTemplate:
    def test_binds_inline_and_downloadable_chart_tools(self):
        tools = _template()["recommended_tools"]
        # Inline (recharts) ET fichier téléchargeable (Plotly HTML).
        assert "business_intelligence.tool_create_chart" in tools
        assert "visualization.tool_visualize_data" in tools
        # Outils text_to_sql de base.
        assert "text_to_sql.tool_text_to_sql" in tools
        assert "database.tool_run_sql" in tools

    def test_instruction_drives_embed_chart_workflow(self):
        instr = _template()["agent_instruction"]
        assert "embed_chart" in instr
        assert "tool_create_chart" in instr
        # Insiste sur le passage de l'UUID (garde-fou anti-récidive).
        assert "UUID" in instr

    def test_instruction_routes_downloadable_to_visualize(self):
        instr = _template()["agent_instruction"]
        # Le mode téléchargeable doit pointer vers tool_visualize_data,
        # et interdire d'inventer un lien (dérive "faux HTML" Mistral).
        assert "tool_visualize_data" in instr
        assert "Downloadable" in instr or "downloadable" in instr
        assert "Never invent the link" in instr

    def test_instruction_forbids_already_created(self):
        instr = _template()["agent_instruction"]
        assert "already created" in instr  # doit recréer le chart à chaque fois

    def test_instruction_stays_compact(self):
        instr = _template()["agent_instruction"]
        # Ceiling reflects what this agent now does: SQL + inline charts +
        # dashboard/KPI management + downloadable exports + email (with the
        # "only when asked" guardrail) + anti-hallucination rules. It is heavy
        # for a small model (Mistral-Small) — a leaner-instruction refactor is a
        # worthwhile follow-up — but this still guards against unbounded bloat.
        assert len(instr) < 5600, f"instruction trop longue: {len(instr)} chars"
