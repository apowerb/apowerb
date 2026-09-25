"""Adoption des pastilles « Étape suivante » : règles contre IA (roadmap#88).

L'éditeur envoie un événement par étape suivante — les pastilles montrées de
chaque source, et celle qui a été prise s'il y en a une. On n'en garde que
des totaux par jour × source × type : ni utilisateur, ni graphe, ni libellé,
ni configuration. De quoi dire si l'IA propose mieux que les règles sans
pouvoir reconstituer le travail de quiconque.

Une source a deux sortes de lignes : ``node_type = ""`` porte ses totaux
(étapes où elle a montré au moins une pastille, pastilles montrées, étapes où
l'une des siennes a été prise) ; les autres comptent les prises par type.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite

from apowerb.core.workflow_graph import NodeType
from apowerb.core.workflow_main import workflow_store
from apowerb.core.workflow_suggest import MAX_SUGGESTIONS as MAX_AI_SHOWN

# Plafond de l'éditeur pour les pastilles des règles (``MAX_SUGGESTIONS`` de
# ``nextNodeSuggestions.js`` dans l'UI).
MAX_RULES_SHOWN = 3
SOURCES = ("rules", "ai")
_TOTAL = ""
_TOP_TYPES = 5
_COUNTS = ("steps", "shown", "accepted")


class AcceptedChip(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["rules", "ai"]
    type: NodeType

    @field_validator("type")
    @classmethod
    def _never_a_trigger(cls, value: str) -> str:
        if value == "trigger":
            raise ValueError("a trigger is never suggested as a next step")
        return value


class SuggestEvent(BaseModel):
    """Une étape suivante telle que l'éditeur l'a montrée."""

    model_config = ConfigDict(extra="forbid")

    rules_shown: int = Field(0, ge=0, le=MAX_RULES_SHOWN)
    ai_shown: int = Field(0, ge=0, le=MAX_AI_SHOWN)
    accepted: Optional[AcceptedChip] = None

    @model_validator(mode="after")
    def _coherent(self) -> "SuggestEvent":
        if not (self.rules_shown or self.ai_shown):
            raise ValueError("no chip was shown")
        if self.accepted and not self.shown(self.accepted.source):
            raise ValueError("the accepted chip comes from a source that showed none")
        return self

    def shown(self, source: str) -> int:
        return self.rules_shown if source == "rules" else self.ai_shown


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _insert(dialect: str):
    return postgresql.insert if dialect == "postgresql" else sqlite.insert


def record_event(event: SuggestEvent, *, today: Optional[date] = None) -> None:
    day = (today or _utc_today()).isoformat()
    taken = event.accepted
    rows = []
    for source in SOURCES:
        if not event.shown(source):
            continue
        hit = taken is not None and taken.source == source
        rows.append(
            {"day": day, "source": source, "node_type": _TOTAL,
             "steps": 1, "shown": event.shown(source), "accepted": int(hit)}
        )
        if hit:
            rows.append(
                {"day": day, "source": source, "node_type": taken.type,
                 "steps": 0, "shown": 0, "accepted": 1}
            )

    t = workflow_store.suggest_table
    with workflow_store.engine.begin() as conn:
        stmt = _insert(conn.dialect.name)(t).values(rows)
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[t.c.day, t.c.source, t.c.node_type],
                set_={c: t.c[c] + stmt.excluded[c] for c in _COUNTS},
            )
        )


def read_totals(*, days: int, today: Optional[date] = None) -> dict:
    """Totaux des ``days`` derniers jours, aujourd'hui compris (UTC).

    ``acceptance_rate`` = étapes où une pastille de la source a été prise ÷
    étapes où elle en a montré ; ``None`` tant qu'elle n'en a montré aucune.
    """
    today = today or _utc_today()
    since = today - timedelta(days=days - 1)
    t = workflow_store.suggest_table
    query = (
        select(t.c.source, t.c.node_type, *(func.sum(t.c[c]) for c in _COUNTS))
        .where(t.c.day >= since.isoformat(), t.c.day <= today.isoformat())
        .group_by(t.c.source, t.c.node_type)
    )
    sources = {
        s: {"steps": 0, "shown": 0, "accepted": 0, "acceptance_rate": None, "top_types": []}
        for s in SOURCES
    }
    with workflow_store.engine.begin() as conn:
        for source, node_type, steps, shown, accepted in conn.execute(query):
            entry = sources.get(source)
            if entry is None:
                continue
            if node_type == _TOTAL:
                entry.update(steps=int(steps), shown=int(shown), accepted=int(accepted))
            else:
                entry["top_types"].append({"type": node_type, "accepted": int(accepted)})

    for entry in sources.values():
        entry["top_types"] = sorted(
            entry["top_types"], key=lambda x: (-x["accepted"], x["type"])
        )[:_TOP_TYPES]
        if entry["steps"]:
            entry["acceptance_rate"] = round(entry["accepted"] / entry["steps"], 3)
    return {"days": days, "since": since.isoformat(), "sources": sources}
