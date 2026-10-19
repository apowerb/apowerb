"""``tool_chart_from_question``: a data question answered AND saved as a chart.

An inline chart took three tool calls in a row -- ``tool_text_to_sql``,
``tool_create_chart(query=<its sql_query>)``, then ``embed_chart`` -- and each
one is a full model call that re-sends the instruction and every tool
declaration. This composite runs the first two in one call, with the agent's
own bound tools, so the agent goes straight to ``embed_chart``.

Opt-in per SuperAgent template (``chart_from_question``), and only when the
agent has both tools: the separate tools stay for every other path.
"""
from __future__ import annotations

import inspect
import re
import unicodedata

from apowerb.configs.th2logger import setup_logging

logger = setup_logging(__name__)


def _chart_name(title: str) -> str:
    ascii_title = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return slug[:60] or "chart"


def _make_chart_from_question(text_to_sql, create_chart):
    # The agent-bound tool_text_to_sql records its SQL generator's usage
    # against the session through tool_context; the unbound one has no such
    # parameter.
    pass_context = "tool_context" in inspect.signature(text_to_sql).parameters

    def tool_chart_from_question(
        question: str, chart_type: str, title: str, tool_context=None
    ) -> dict:
        """Answers a data question AND saves the result as a chart, in one call.

        Same query as tool_text_to_sql, saved like tool_create_chart. Next step:
        embed_chart(chart_id) to show it, then describe the data in words.

        Args:
            question: The data question in natural language (what to plot).
            chart_type: bar, line, pie, donut, scatter, area, stat, table or histogram.
            title: Describes the content (e.g. "Parcels by department").

        Returns:
            dict with chart_id, sql_query, row_count and data (the plotted rows);
            on failure success is false with the error, and no chart is saved.
        """
        extra = {"tool_context": tool_context} if pass_context else {}
        result = text_to_sql(question=question, include_sql=True, **extra)
        if not result.get("success"):
            return result
        chart = create_chart(
            name=_chart_name(title), title=title, chart_type=chart_type,
            query=result["sql_query"],
        )
        if not chart.get("success"):
            return {**result, "success": False,
                    "error": f"Query ran but the chart was not saved: {chart.get('error')}"}
        return {
            **result,
            "chart_id": chart["chart_id"],
            "title": chart.get("title", title),
            "chart_type": chart.get("chart_type", chart_type),
            "message": "Chart saved. Call embed_chart(chart_id) to show it, then describe the data in words.",
        }

    return tool_chart_from_question


def bind_chart_from_question(agent_name: str, tools_funcs: list, enabled: bool) -> list:
    """Add ``tool_chart_from_question`` when enabled and both building blocks are bound."""
    if not enabled:
        return tools_funcs
    by_name = {getattr(f, "__name__", ""): f for f in tools_funcs}
    text_to_sql, create_chart = by_name.get("tool_text_to_sql"), by_name.get("tool_create_chart")
    if not (text_to_sql and create_chart) or "tool_chart_from_question" in by_name:
        return tools_funcs
    logger.info("[TO_AGENT] tool_chart_from_question added for %s", agent_name)
    return [*tools_funcs, _make_chart_from_question(text_to_sql, create_chart)]
