"""Colonnes requises d'une source dataset, renommages purs et mapping de colonnes."""

from __future__ import annotations

import pytest

from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.lifecycle import (
    SourceColumn,
    apply_mapping,
    file_mapping,
    pure_renames,
    root_dataset_source,
    source_columns,
)
from apowerb.bi.spec.models import DeriveStep, SourceSpec
from apowerb.bi.spec.user_templates import freeze
from apowerb.bi.spec.templates import stock_pilotage
from tests.helpers.bi_template_env import dashboard_from_spec


def test_simulated_spec_lists_the_real_dataset_columns_only():
    dashboard = dashboard_from_spec(stock_pilotage("ds-1"))
    assert source_columns(dashboard) == [
        SourceColumn("semaine", "Semaine", "date", True, "semaine"),
        SourceColumn("famille", "Famille", "string", True, "famille"),
        SourceColumn("ventes", "Ventes", "number", True, "ventes"),
    ]


def test_stock_spec_lists_stock_threshold_and_lead_time():
    spec = stock_pilotage("ds-1", stock="stock", safety_column="seuil_securite", lead_time_column="delai_fournisseur_sem")
    columns = {c.key: c for c in source_columns(dashboard_from_spec(spec))}
    assert set(columns) == {"semaine", "famille", "ventes", "stock", "seuil_securite", "delai_fournisseur_sem"}
    assert columns["stock"].type == "number" and columns["stock"].required
    assert all(c.required for c in columns.values())


def test_unreferenced_dataset_field_is_optional():
    dashboard = dashboard_from_spec(stock_pilotage("ds-1"))
    root = dashboard.sources[0]
    extra = root.model_copy(update={"fields": [*root.fields, root.fields[2].model_copy(update={"name": "prix", "label": "Prix"})]})
    dashboard = dashboard.model_copy(update={"sources": [extra, *dashboard.sources[1:]]})
    prix = next(c for c in source_columns(dashboard) if c.key == "prix")
    assert prix.required is False


def test_leading_pure_renames_are_not_business_logic():
    spec = stock_pilotage("ds-1")
    renames = [DeriveStep(column="semaine", expr='"week"'), DeriveStep(column="ventes", expr='"qty"')]
    spec["sources"][0]["transforms"] = [*(r.model_dump() for r in renames), *spec["sources"][0]["transforms"]]
    dashboard = dashboard_from_spec(spec)
    root = root_dataset_source(dashboard.sources)
    mapping, rest = pure_renames(root.transforms)
    assert mapping == {"semaine": "week", "ventes": "qty"}
    assert rest[0].op == "naive_forecast"
    columns = {c.key: c.current for c in source_columns(dashboard)}
    assert columns == {"semaine": "week", "famille": "famille", "ventes": "qty"}


def test_apply_mapping_prepends_a_derive_only_for_renamed_columns():
    spec = stock_pilotage("ds-1")
    transforms = dashboard_from_spec(spec).sources[0].transforms
    out = apply_mapping(transforms, {"semaine": "week", "famille": "famille", "ventes": None})
    assert [(t.column, t.expr) for t in out[:1]] == [("semaine", '"week"')]
    assert out[1:] == transforms


def test_one_root_dataset_source_children_do_not_count():
    spec = stock_pilotage("ds-1", stock="stock")
    dashboard = dashboard_from_spec(spec)
    assert [s.name for s in dashboard.sources][:3] == ["stocks", "plan", "horizon"]
    assert root_dataset_source(dashboard.sources).name == "stocks"


def test_zero_or_two_dataset_sources_are_refused():
    sql = SourceSpec(name="a", kind="sql", connection_config_id="c", query="select 1")
    two = [SourceSpec(name="a", kind="dataset", dataset_id="x"), SourceSpec(name="b", kind="dataset", dataset_id="y")]
    for sources in ([sql], two):
        with pytest.raises(SpecError):
            root_dataset_source(sources)


def test_column_swap_is_refused():
    with pytest.raises(SpecError):
        apply_mapping([], {"semaine": "famille", "famille": "semaine"})


def _error(call, *args, **kwargs) -> SpecError:
    with pytest.raises(SpecError) as caught:
        call(*args, **kwargs)
    return caught.value


def test_lifecycle_errors_follow_the_dashboard_language():
    sql = SourceSpec(name="a", kind="sql", connection_config_id="c", query="select 1")
    assert str(_error(root_dataset_source, [sql])).startswith("Le tableau doit avoir exactement une source")
    assert str(_error(root_dataset_source, [sql], lang="en")) == (
        "The dashboard must have exactly one file source (dataset); it has 0."
    )
    swap = {"semaine": "famille", "famille": "semaine"}
    assert str(_error(apply_mapping, [], swap)).startswith("Un échange de colonnes")
    assert str(_error(apply_mapping, [], swap, lang="en")) == "Swapping columns (A to B and B to A) is not supported."


def test_file_mapping_errors_follow_the_dashboard_language():
    dashboard = dashboard_from_spec(stock_pilotage("ds-1"))
    french = _error(file_mapping, dashboard, {"inconnue": "x"})
    english = _error(file_mapping, dashboard.model_copy(update={"locale": "en"}), {"inconnue": "x"})
    assert french.errors == ["Colonne inconnue pour ce tableau : inconnue."]
    assert english.errors == ["Unknown column for this dashboard: inconnue."]


def test_freezing_a_legacy_dashboard_is_refused_in_the_dashboard_language():
    legacy = dashboard_from_spec(stock_pilotage("ds-1")).model_copy(update={"spec_version": 1})
    assert str(_error(freeze, legacy, "L", "", "private")) == (
        "Seul un tableau de bord à la spécification v2 peut devenir un modèle."
    )
    english = legacy.model_copy(update={"locale": "en"})
    assert str(_error(freeze, english, "L", "", "private")) == "Only a spec v2 dashboard can become a template."


def test_route_messages_follow_the_dashboard_language():
    from apowerb.bi.spec.router import _missing_in_new_file, _template_not_found, _unprocessable

    assert _template_not_found("t1", None).detail == "Gabarit introuvable : t1"
    assert _template_not_found("t1", "en").detail == "Template not found: t1"
    assert _template_not_found("t1", "en").status_code == 404

    columns = [SourceColumn("ventes", "Ventes", "number", True, "ventes")]
    assert _missing_in_new_file(columns, {"ventes": "qty"}, ["a", "b"], None) == [
        "La colonne « qty » (Ventes) est introuvable dans le nouveau fichier. Colonnes disponibles : a, b."
    ]
    assert _missing_in_new_file(columns, {"ventes": "qty"}, ["a", "b"], "en") == [
        "The column “qty” (Ventes) is not in the new file. Available columns: a, b."
    ]
    assert _missing_in_new_file(columns, {}, ["a"], None)[0].startswith("La colonne (Ventes) est introuvable")
    assert _missing_in_new_file(columns, {"ventes": "a"}, ["a"], "en") == []

    exc = SpecError("bonjour", en="hello")
    assert _unprocessable(exc).detail == ["bonjour"]
    assert _unprocessable(exc, "en").detail == ["hello"]
