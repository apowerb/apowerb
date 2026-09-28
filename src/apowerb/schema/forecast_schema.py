"""Pydantic schema for POST /api/v1/forecast — mirrors the th2forecast request
shape fixed by the contract (~/tmp/th2fc/CONTRAT.md), validated here so a
malformed request never reaches th2forecast at all."""

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# th2forecast's own MAX_HORIZON default (see contract); the route validates
# against this ceiling too so an oversized horizon fails fast, locally, with
# the same limit th2forecast would enforce (it can still return 413 for a
# smaller-but-still-too-large deployment-specific limit).
MAX_HORIZON = 366
# th2forecast's own MAX_ROWS default: rejecting larger payloads here avoids
# relaying tens of megabytes only for th2forecast to answer 413.
MAX_DATA_ROWS = 100_000

ALLOWED_MODELS = frozenset({"prophet", "arima", "ets", "snaive", "naive", "auto"})
ALLOWED_FREQUENCIES = frozenset({"day", "week", "month", "quarter", "year"})


class ForecastRequestSchema(BaseModel):
    """Body of POST /api/v1/forecast — relayed to th2forecast almost verbatim."""

    data: list[dict[str, Any]] = Field(..., min_length=1, max_length=MAX_DATA_ROWS)
    date_var: str = Field(..., min_length=1)
    target_var: str = Field(..., min_length=1)
    group_var: str | None = None
    horizon: int = Field(..., ge=1, le=MAX_HORIZON)
    frequency: str | None = None
    models: list[str] = Field(default_factory=lambda: ["prophet"], min_length=1)
    confidence_levels: list[float] | None = Field(default_factory=lambda: [0.8, 0.95])
    holidays_country: str | None = None
    # Contexte métier (th2forecast : événements appris comme covariables et
    # scénarios « et si »). Sans ces champs, Pydantic les jetterait en silence ;
    # leur contenu est validé par th2forecast, qui répond 400 avec un message clair.
    events: list[dict[str, Any]] | None = Field(None, max_length=20)
    scenarios: list[dict[str, Any]] | None = Field(None, max_length=5)

    @field_validator("models")
    @classmethod
    def _models_in_allowed_set(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - ALLOWED_MODELS)
        if unknown:
            raise ValueError(
                f"modèle(s) inconnu(s) : {', '.join(unknown)} ; autorisés : "
                f"{', '.join(sorted(ALLOWED_MODELS))}"
            )
        return value

    @field_validator("frequency")
    @classmethod
    def _frequency_in_allowed_set(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_FREQUENCIES:
            raise ValueError(
                f"frequency invalide : {value!r} ; autorisées : "
                f"{', '.join(sorted(ALLOWED_FREQUENCIES))} ou null"
            )
        return value

    @field_validator("confidence_levels")
    @classmethod
    def _confidence_levels_in_range(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("confidence_levels ne peut pas être une liste vide")
        for level in value:
            if not (0 < level < 1):
                raise ValueError(f"confidence_levels doit être dans ]0, 1[, reçu {level!r}")
        return value

    @field_validator("data")
    @classmethod
    def _data_rows_are_non_empty_objects(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for i, row in enumerate(value):
            if not row:
                raise ValueError(f"data[{i}] est une ligne vide")
        return value


class ForecastInterpretSchema(BaseModel):
    """Body of POST /api/v1/forecast/interpret — free text to dated events."""

    text: str = Field(..., min_length=3, max_length=2000)
    history_start: date
    history_end: date
    horizon_end: date
    frequency: str
    groups: list[str] = Field(default_factory=list, max_length=200)
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=20)

    @field_validator("frequency")
    @classmethod
    def _frequency_in_allowed_set(cls, value: str) -> str:
        if value not in ALLOWED_FREQUENCIES:
            raise ValueError(f"frequency invalide : {value!r}")
        return value

    @model_validator(mode="after")
    def _dates_in_order(self) -> "ForecastInterpretSchema":
        if not self.history_start <= self.history_end < self.horizon_end:
            raise ValueError("attendu : history_start <= history_end < horizon_end")
        return self
