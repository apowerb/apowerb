"""
bi/spec/frames.py
-----------------
Materialised source frames: column naming, type coercion, field inference and
provenance tracking shared by the loaders, the transforms and the visuals.
"""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

import pandas as pd

from apowerb.bi.spec.models import FieldSpec, Provenance

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")
_FR_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_FR_NUMBER_RE = re.compile(r"^-?\d+,\d+$")
_IDENT_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DATE_SAMPLE = 50

# Severity order when several inputs are combined: simulated > forecast > real.
_RANK = {Provenance.REAL: 0, Provenance.FORECAST: 1, Provenance.SIMULATED: 2}


def combine(provenances: Iterable[Provenance]) -> Provenance:
    """Provenance of something computed from several inputs."""
    best = Provenance.REAL
    for p in provenances:
        if _RANK[p] > _RANK[best]:
            best = p
    return best


def identifiers(sql: str) -> set[str]:
    """Lower-cased identifier-like tokens of a SQL fragment (strings excluded)."""
    without_strings = re.sub(r"'(?:[^']|'')*'", " ", sql)
    quoted = {m.lower() for m in re.findall(r'"((?:[^"]|"")*)"', without_strings)}
    plain = re.sub(r'"(?:[^"]|"")*"', " ", without_strings)
    return {t.lower() for t in _IDENT_TOKEN_RE.findall(plain)} | quoted


def referenced_columns(sql: str, columns: Iterable[str]) -> list[str]:
    """Columns (by exact name, case-insensitive) mentioned in a SQL fragment."""
    tokens = identifiers(sql)
    return [c for c in columns if c.lower() in tokens]


def normalize_name(raw: object, taken: set[str]) -> str:
    """snake_case ASCII identifier for a raw column header, unique in ``taken``."""
    text = unicodedata.normalize("NFKD", str(raw)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "col"
    if text[0].isdigit():
        text = "c_" + text
    text = text[:56]
    candidate, i = text, 2
    while candidate in taken:
        candidate = f"{text}_{i}"
        i += 1
    taken.add(candidate)
    return candidate


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename every column to a valid, unique spec identifier."""
    taken: set[str] = set()
    names = [normalize_name(c, taken) for c in df.columns]
    out = df.copy()
    out.columns = names
    return out


def _parse_dates(series: pd.Series) -> pd.Series | None:
    sample = series.dropna().head(_DATE_SAMPLE)
    if sample.empty:
        return None
    values = [v for v in sample]
    if all(isinstance(v, (_dt.date, _dt.datetime, pd.Timestamp)) for v in values):
        parsed = pd.to_datetime(series, errors="coerce")
    elif all(isinstance(v, str) and _ISO_DATE_RE.match(v.strip()) for v in values):
        parsed = pd.to_datetime(series, errors="coerce", utc=True).dt.tz_localize(None)
    elif all(isinstance(v, str) and _FR_DATE_RE.match(v.strip()) for v in values):
        parsed = pd.to_datetime(series, errors="coerce", dayfirst=True, format="%d/%m/%Y")
    else:
        return None
    if parsed.notna().sum() < 0.9 * series.notna().sum():
        return None
    return parsed


def coerce_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise column names and infer proper dtypes (dates, numbers, bools)."""
    df = normalize_columns(df)
    for col in list(df.columns):
        s = df[col]
        if s.dtype == object:
            non_null = s.dropna()
            if len(non_null) and non_null.map(lambda v: isinstance(v, Decimal)).all():
                df[col] = pd.to_numeric(s.map(lambda v: None if v is None else float(v)))
                continue
            if len(non_null) and non_null.map(lambda v: isinstance(v, bool)).all():
                df[col] = s.astype("boolean")
                continue
            parsed = _parse_dates(s)
            if parsed is not None:
                df[col] = parsed
                continue
            if len(non_null) and non_null.map(
                lambda v: isinstance(v, str) and bool(_FR_NUMBER_RE.match(v.strip()))
            ).all():
                df[col] = pd.to_numeric(s.map(lambda v: v.strip().replace(",", ".") if isinstance(v, str) else v))
        elif isinstance(s.dtype, pd.DatetimeTZDtype):
            df[col] = s.dt.tz_convert("UTC").dt.tz_localize(None)
    return df


def field_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"
    if pd.api.types.is_numeric_dtype(series):
        return "number"
    return "string"


def infer_fields(
    df: pd.DataFrame,
    prov: dict[str, Provenance],
    declared: list[FieldSpec],
) -> list[FieldSpec]:
    """Fields of a frame: inferred, then overridden by the declared ones.

    A declared ``label`` / ``format`` / ``role`` always wins. Because the spec
    is persisted with its defaults filled in, a declared ``type`` of
    ``"string"`` and a declared provenance of ``real`` are indistinguishable
    from "not set": only a non-default value overrides the inferred one.
    """
    overrides = {f.name: f for f in declared}
    out: list[FieldSpec] = []
    for col in df.columns:
        ftype = field_type(df[col])
        role = "date" if ftype == "date" else ("measure" if ftype == "number" else "dimension")
        spec = FieldSpec(
            name=col,
            type=ftype,  # type: ignore[arg-type]
            role=role,  # type: ignore[arg-type]
            provenance=prov.get(col, Provenance.REAL),
        )
        ov = overrides.get(col)
        if ov is not None:
            update: dict = {}
            if ov.label is not None:
                update["label"] = ov.label
            if ov.format is not None:
                update["format"] = ov.format
            if ov.role is not None:
                update["role"] = ov.role
            if ov.type != "string":
                update["type"] = ov.type
            if ov.provenance != Provenance.REAL:
                update["provenance"] = ov.provenance
            spec = spec.model_copy(update=update)
        out.append(spec)
    return out


@dataclass
class MatSource:
    """A source after loading and transforms, ready to be filtered and queried.

    ``df`` is shared through the cache and must never be mutated in place.
    """

    name: str
    df: pd.DataFrame
    prov: dict[str, Provenance]
    fields: list[FieldSpec]
    warnings: list[str] = field(default_factory=list)
    # forecast output column -> error message, when the engine call failed
    failed_columns: dict[str, str] = field(default_factory=dict)
    token: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def columns(self) -> list[str]:
        return list(self.df.columns)

    def provenance_of(self, col: str) -> Provenance:
        for f in self.fields:
            if f.name == col:
                return f.provenance
        return Provenance.REAL

    def date_field(self) -> str | None:
        """First date-typed column (declared role first)."""
        for f in self.fields:
            if f.role == "date" and f.name in self.df.columns:
                return f.name
        for f in self.fields:
            if f.type == "date" and f.name in self.df.columns:
                return f.name
        return None

    def real_last_date(self, column: str) -> pd.Timestamp | None:
        """Last date of ``column`` that carries a real value.

        Rows appended for forecast periods have no real numeric value, so they
        do not move this date. Falls back to the last date overall.
        """
        if column not in self.df.columns:
            return None
        dates = pd.to_datetime(self.df[column], errors="coerce")
        real_numeric = [
            f.name
            for f in self.fields
            if f.type == "number" and f.provenance == Provenance.REAL and f.name in self.df.columns
        ]
        if real_numeric:
            has_value = self.df[real_numeric].notna().any(axis=1)
            real_dates = dates[has_value]
            if real_dates.notna().any():
                return real_dates.max()
        return dates.max() if dates.notna().any() else None
