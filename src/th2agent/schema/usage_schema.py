"""Pydantic schemas for the LLM usage endpoints.

Cost fields are ``Optional[float]``: ``None`` means "this model is not in
the pricing grid", which the UI renders as "—". It never means "free" --
free models are priced at an explicit ``0.0`` in
``configs/model_pricing.yaml``.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class UsageTotals(BaseModel):
    """Aggregated usage over a window (whole scope, one agent, ...)."""

    calls: int
    invocations: int
    input_tokens: int
    output_tokens: int
    thoughts_tokens: int
    cached_tokens: int
    total_tokens: int
    cost_eur: Optional[float] = None
    avg_tokens_per_call: float = 0.0
    avg_turns_per_invocation: float = 0.0
    cache_hit_rate: float = 0.0


class PerAgentUsage(BaseModel):
    """Aggregated usage for one agent (all its models merged)."""

    agent_id: int
    agent_name: str
    calls: int
    invocations: int
    input_tokens: int
    output_tokens: int
    thoughts_tokens: int
    cached_tokens: int
    total_tokens: int
    cost_eur: Optional[float] = None
    avg_tokens_per_call: float = 0.0
    models: list[str] = []


class SeriesPoint(BaseModel):
    """One time bucket -- a calendar day (``YYYY-MM-DD``) or an hour
    (``YYYY-MM-DDTHH:00``), always in Europe/Paris. Only non-empty
    buckets are returned; the front-end fills the gaps.
    """

    bucket: str
    calls: int
    total_tokens: int
    cost_eur: Optional[float] = None


class PerModelUsage(BaseModel):
    model: str
    calls: int
    total_tokens: int
    cost_eur: Optional[float] = None


class PerSourceUsage(BaseModel):
    invocation_source: Optional[str] = None
    calls: int
    total_tokens: int
    cost_eur: Optional[float] = None


class HeatmapCell(BaseModel):
    """Usage by weekday x hour, Europe/Paris. ``dow``: 0 = Monday."""

    dow: int
    hour: int
    calls: int
    total_tokens: int


class UsageSummaryResponse(BaseModel):
    days: int
    granularity: str
    totals: UsageTotals
    previous_totals: UsageTotals
    per_agent: list[PerAgentUsage]
    per_series: list[SeriesPoint]
    per_model: list[PerModelUsage]
    per_source: list[PerSourceUsage]
    heatmap: list[HeatmapCell]


class PerSessionUsage(BaseModel):
    session_id: Optional[str] = None
    calls: int
    invocations: int
    total_tokens: int
    cost_eur: Optional[float] = None
    first_at: Optional[str] = None
    last_at: Optional[str] = None


class TurnProfilePoint(BaseModel):
    """Average prompt size at turn N of an invocation.

    This is the clearest signal of WHY tokens burn: the conversation
    history and every tool result are re-sent in full at each turn, so
    ``avg_input_tokens`` grows with ``turn_index``.
    """

    turn_index: int
    calls: int
    avg_input_tokens: float
    avg_output_tokens: float


class PerToolUsage(BaseModel):
    """Prompt growth attributable to one tool.

    A tool call at turn N does not cost tokens at turn N -- its result is
    re-sent as part of turn N+1's prompt. ``induced_input_tokens`` is
    therefore the input growth measured on the following turn.
    """

    tool: str
    turns: int
    induced_input_tokens: int
    avg_induced_tokens: float


class TopInvocation(BaseModel):
    invocation_id: str
    turns: int
    total_tokens: int
    cost_eur: Optional[float] = None
    started_at: Optional[str] = None
    tools: list[str] = []


class UsageDrivers(BaseModel):
    """What makes the tokens burn. All fields degrade to empty/zero on
    rows written before the drivers instrumentation (``invocation_id``
    and ``tool_names`` are NULL there) -- the UI must say "no
    instrumented data on this window", not show an error.
    """

    avg_turns_per_invocation: float = 0.0
    max_turns: int = 0
    turn_profile: list[TurnProfilePoint] = []
    per_tool: list[PerToolUsage] = []
    top_invocations: list[TopInvocation] = []


class AgentUsageDetailResponse(BaseModel):
    agent_id: int
    agent_name: str
    days: int
    totals: UsageTotals
    per_hour: list[SeriesPoint]
    per_model: list[PerModelUsage]
    per_source: list[PerSourceUsage]
    per_session: list[PerSessionUsage]
    drivers: UsageDrivers


class QuotaStatusResponse(BaseModel):
    """Quota mensuel de tokens sur le modèle thaink2 mutualisé.

    ``enabled=False`` quand le serveur ne sert pas de modèle par défaut :
    le front n'affiche alors aucun bandeau. Les champs ``limit_tokens`` /
    ``remaining_tokens`` / ``percent_used`` sont ``None`` sur un plan
    illimité — une valeur numérique laisserait croire à un plafond.
    """

    enabled: bool
    used_tokens: int = 0
    limit_tokens: Optional[int] = None
    remaining_tokens: Optional[int] = None
    percent_used: Optional[float] = None
    exceeded: bool = False
    warning: bool = False
    plan: Optional[str] = None
    resets_at: Optional[datetime] = None
