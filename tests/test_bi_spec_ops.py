"""Dashboard spec v2: patch operations and static validation."""

from __future__ import annotations

import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, GridPosition
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.models import (
    AddVisualOp,
    FilterSpec,
    KpiSpec,
    MeasureSpec,
    MoveComponentOp,
    RemoveComponentOp,
    RemoveFilterOp,
    RemoveMeasureOp,
    RemoveSourceOp,
    SeriesSpec,
    SetTitleOp,
    SourceSpec,
    UpdateVisualOp,
    UpsertFilterOp,
    UpsertMeasureOp,
    UpsertSourceOp,
    VisualSpec,
)
from apowerb.bi.spec.ops import apply_ops
from apowerb.bi.spec.validate import find_cycle_members, validate_spec
from tests.helpers.bi_spec_data import build_dashboard, combo, dataset_source, kpi, pipeline_source


def empty() -> Dashboard:
    return Dashboard(title="Vide", created_by="o@example.com")


def with_source() -> Dashboard:
    return apply_ops(empty(), [UpsertSourceOp(source=pipeline_source())])


class TestOps:
    def test_upsert_source_sets_spec_version_2_and_does_not_mutate_input(self):
        base = empty()
        out = apply_ops(base, [UpsertSourceOp(source=pipeline_source())])
        assert out.spec_version == 2 and [s.name for s in out.sources] == ["ventes"]
        assert base.spec_version == 1 and base.sources == []
        assert out.version == base.version + 1

    def test_upsert_source_replaces_by_name(self):
        d = with_source()
        out = apply_ops(d, [UpsertSourceOp(source=dataset_source("ventes").model_copy(update={"label": "Nouveau"}))])
        assert len(out.sources) == 1 and out.sources[0].label == "Nouveau"

    def test_add_visual_auto_positions_below_the_grid(self):
        d = with_source()
        d = apply_ops(d, [AddVisualOp(visual=kpi("ventes_tot"), component_id="k1")])
        first = d.components[0]
        assert first.component_type == ComponentType.VISUAL and first.position.row == 0
        d = apply_ops(d, [AddVisualOp(visual=combo("ventes_tot"), component_id="c1")])
        second = d.components[1]
        assert second.position.row == first.position.row + first.position.height
        assert second.position.col == 0
        d = apply_ops(d, [AddVisualOp(visual=combo("ventes_tot"), position={"row": 9, "col": 6, "width": 6, "height": 3})])
        assert d.components[2].position == GridPosition(row=9, col=6, width=6, height=3)
        assert d.components[2].id  # generated

    def test_add_visual_duplicate_id_and_bad_position(self):
        d = apply_ops(with_source(), [AddVisualOp(visual=kpi("ventes_tot"), component_id="k1")])
        with pytest.raises(SpecError, match="existe déjà"):
            apply_ops(d, [AddVisualOp(visual=kpi("ventes_tot"), component_id="k1")])
        with pytest.raises(SpecError, match="Position"):
            apply_ops(d, [AddVisualOp(visual=kpi("ventes_tot"), position={"width": 99})])
        with pytest.raises(SpecError, match="inconnue"):
            apply_ops(d, [AddVisualOp(visual=kpi("ventes_tot"), position={"depth": 1})])

    def test_update_visual_shallow_merge_and_revalidation(self):
        d = apply_ops(with_source(), [AddVisualOp(visual=combo("ventes_tot"), component_id="c1")])
        out = apply_ops(d, [UpdateVisualOp(component_id="c1", changes={"title": "Titre", "series": [{"key": "prevu_tot", "mark": "line"}]})])
        v = out.components[0].visual
        assert v.title == "Titre" and v.x == "semaine"  # untouched fields stay
        assert [s.key for s in v.series] == ["prevu_tot"] and v.series[0].mark == "line"

    def test_update_visual_errors(self):
        d = apply_ops(with_source(), [AddVisualOp(visual=combo("ventes_tot"), component_id="c1")])
        with pytest.raises(SpecError, match="inconnu"):
            apply_ops(d, [UpdateVisualOp(component_id="c1", changes={"titel": "x"})])
        with pytest.raises(SpecError, match="type"):
            apply_ops(d, [UpdateVisualOp(component_id="c1", changes={"type": "camembert"})])
        with pytest.raises(SpecError, match="introuvable"):
            apply_ops(d, [UpdateVisualOp(component_id="zzz", changes={"title": "x"})])
        with pytest.raises(SpecError, match="source inconnue"):
            apply_ops(d, [UpdateVisualOp(component_id="c1", changes={"source": "fantome"})])

    def test_remove_and_move_component(self):
        d = apply_ops(with_source(), [
            AddVisualOp(visual=kpi("ventes_tot"), component_id="k1"),
            AddVisualOp(visual=kpi("nsl"), component_id="k2"),
        ])
        moved = apply_ops(d, [MoveComponentOp(component_id="k2", position={"row": 0, "col": 3})])
        pos = {c.id: c.position for c in moved.components}["k2"]
        assert (pos.row, pos.col, pos.width) == (0, 3, 3)  # width/height kept
        removed = apply_ops(moved, [RemoveComponentOp(component_id="k1")])
        assert [c.id for c in removed.components] == ["k2"]
        with pytest.raises(SpecError, match="introuvable"):
            apply_ops(removed, [RemoveComponentOp(component_id="k1")])
        with pytest.raises(SpecError, match="introuvable"):
            apply_ops(removed, [MoveComponentOp(component_id="nope", position={"row": 1})])

    def test_measures_upsert_and_remove(self):
        d = with_source()
        out = apply_ops(d, [UpsertMeasureOp(source="ventes", measure=MeasureSpec(name="marge", label="Marge", expr="SUM(ventes) * 0.2"))])
        assert "marge" in [m.name for m in out.sources[0].measures]
        replaced = apply_ops(out, [UpsertMeasureOp(source="ventes", measure=MeasureSpec(name="marge", label="Marge 2", expr="SUM(ventes)"))])
        assert [m.label for m in replaced.sources[0].measures if m.name == "marge"] == ["Marge 2"]
        removed = apply_ops(replaced, [RemoveMeasureOp(source="ventes", name="marge")])
        assert "marge" not in [m.name for m in removed.sources[0].measures]
        with pytest.raises(SpecError, match="Mesure introuvable"):
            apply_ops(removed, [RemoveMeasureOp(source="ventes", name="marge")])
        with pytest.raises(SpecError, match="Source introuvable"):
            apply_ops(d, [UpsertMeasureOp(source="rien", measure=MeasureSpec(name="m", label="M", expr="COUNT(*)"))])

    def test_removing_a_measure_used_by_a_visual_is_refused(self):
        d = apply_ops(with_source(), [AddVisualOp(visual=kpi("nsl"), component_id="k1")])
        with pytest.raises(SpecError, match="nsl"):
            apply_ops(d, [RemoveMeasureOp(source="ventes", name="nsl")])

    def test_filters_upsert_and_remove(self):
        d = with_source()
        flt = FilterSpec(id="famille", label="Famille", field="famille")
        out = apply_ops(d, [UpsertFilterOp(filter=flt)])
        assert [f.id for f in out.filters] == ["famille"]
        out = apply_ops(out, [UpsertFilterOp(filter=flt.model_copy(update={"label": "Fam"}))])
        assert [f.label for f in out.filters] == ["Fam"]
        assert apply_ops(out, [RemoveFilterOp(id="famille")]).filters == []
        with pytest.raises(SpecError, match="Filtre introuvable"):
            apply_ops(d, [RemoveFilterOp(id="famille")])

    def test_remove_source_used_by_a_visual_is_refused(self):
        d = apply_ops(with_source(), [AddVisualOp(visual=kpi("nsl"), component_id="k1")])
        with pytest.raises(SpecError, match="source inconnue"):
            apply_ops(d, [RemoveSourceOp(name="ventes")])
        with pytest.raises(SpecError, match="Source introuvable"):
            apply_ops(d, [RemoveSourceOp(name="autre")])
        d2 = apply_ops(d, [RemoveComponentOp(component_id="k1"), RemoveSourceOp(name="ventes")])
        assert d2.sources == []

    def test_set_title(self):
        out = apply_ops(empty(), [SetTitleOp(title="Pilotage", description="Vue hebdo")])
        assert (out.title, out.description) == ("Pilotage", "Vue hebdo")
        assert out.spec_version == 1  # nothing v2 yet
        assert apply_ops(out, [SetTitleOp(title="Autre")]).description == "Vue hebdo"

    def test_atomic_on_error_and_message_names_the_operation(self):
        d = with_source()
        with pytest.raises(SpecError, match=r"Opération 2 \(remove_component\)"):
            apply_ops(d, [SetTitleOp(title="Nouveau"), RemoveComponentOp(component_id="zzz")])
        assert d.title == "Test spec" or d.title == empty().title  # input untouched

    def test_validation_errors_are_listed(self):
        with pytest.raises(SpecError) as exc:
            apply_ops(with_source(), [
                AddVisualOp(visual=VisualSpec(type="kpi", source="fantome", kpi=KpiSpec(measure="x"))),
                AddVisualOp(visual=VisualSpec(type="bar", source="ventes", x="semaine", series=[SeriesSpec(key="nope")]), component_id="b"),
            ])
        assert any("fantome" in e for e in exc.value.errors)

    def test_roundtrip_json_keeps_the_spec(self):
        d = apply_ops(with_source(), [AddVisualOp(visual=combo("ventes_tot", title="T"), component_id="c1"), UpsertFilterOp(filter=FilterSpec(id="famille", label="Famille", field="famille"))])
        again = Dashboard.model_validate_json(d.model_dump_json())
        assert again == d


class TestValidate:
    def test_clean_spec_has_no_error(self):
        d = build_dashboard({"k": kpi("nsl"), "c": combo("ventes_tot", "prevu_tot")})
        assert validate_spec(d) == []

    def test_unknown_source_measure_and_filter_references(self):
        d = build_dashboard({
            "a": VisualSpec(type="kpi", source="fantome", kpi=KpiSpec(measure="x")),
            "b": kpi("inconnue"),
            "c": VisualSpec(type="bar", source="ventes", x="semaine", series=[SeriesSpec(key="ventes_tot")], ignore_filters=["nope"], click_filters={"zzz": "semaine"}, highlight_filter="yyy"),
            "d": VisualSpec(type="text", source="ventes", text="{{ventes_tot}} {{filters.absent}} {{period.mid}} {{nsl|bizarre}}"),
            "e": VisualSpec(type="kpi", source="ventes"),
        })
        errors = validate_spec(d)
        joined = "\n".join(errors)
        for needle in ("fantome", "inconnue", "nope", "zzz", "yyy", "filters.absent", "period.mid", "bizarre", "kpi"):
            assert needle in joined, (needle, errors)

    def test_source_cycle(self):
        a = SourceSpec(name="a", kind="source", parent="b")
        b = SourceSpec(name="b", kind="source", parent="a")
        c = dataset_source("c")
        d = Dashboard(title="x", sources=[a, b, c], spec_version=2)
        assert find_cycle_members(d.sources) == {"a", "b"}
        assert any("Cycle" in e for e in validate_spec(d))

    def test_filter_field_absent_everywhere(self):
        from apowerb.bi.spec.models import AggregateStep

        closed = dataset_source("agg", transforms=[AggregateStep(group_by=["famille"], aggregates={"total": "SUM(ventes)"})])
        d = Dashboard(title="x", sources=[closed], spec_version=2, filters=[
            FilterSpec(id="ok", label="Famille", field="famille"),
            FilterSpec(id="ko", label="Zone", field="zone"),
        ])
        errors = validate_spec(d)
        assert any("« zone »" in e and "aucune source" in e for e in errors)
        assert not any("« famille »" in e for e in errors)

    def test_open_dataset_source_cannot_prove_a_field_missing(self):
        d = Dashboard(title="x", sources=[dataset_source("s")], spec_version=2, filters=[FilterSpec(id="f", label="F", field="whatever")])
        assert validate_spec(d) == []

    def test_forbidden_sql_and_bad_sources(self):
        from apowerb.bi.spec.models import DeriveStep

        bad = SourceSpec(name="s", kind="sql", connection_config_id="c", query="DROP TABLE t")
        worse = dataset_source("w", transforms=[DeriveStep(column="x", expr="1; DROP TABLE y")], measures=[MeasureSpec(name="m", label="M", expr="read_csv('/etc/passwd')")])
        nop = SourceSpec(name="n", kind="dataset")
        errors = validate_spec(Dashboard(title="x", sources=[bad, worse, nop], spec_version=2))
        joined = "\n".join(errors).lower()
        assert "drop" in joined and "read_csv" in joined and "dataset_id" in joined and "instruction" in joined

    def test_missing_required_parts(self):
        d = build_dashboard({
            "a": VisualSpec(type="combo", source="ventes", series=[SeriesSpec(key="ventes_tot")]),
            "b": VisualSpec(type="combo", source="ventes", x="semaine"),
            "c": VisualSpec(type="waterfall", source="ventes"),
            "d": VisualSpec(type="note"),
            "e": VisualSpec(type="bar", x="semaine", series=[SeriesSpec(key="ventes_tot")]),
        })
        errors = validate_spec(d)
        assert len(errors) >= 5


def test_kpi_sparkline_must_name_a_measure():
    d = with_source()
    with pytest.raises(SpecError, match="kpi.sparkline"):
        apply_ops(d, [AddVisualOp(visual=VisualSpec(type="kpi", source="ventes",
                                                     kpi=KpiSpec(measure="ventes_tot", sparkline="fantome")))])


def test_table_total_measure_must_name_a_measure():
    from apowerb.bi.spec.models import ColumnSpec

    d = with_source()
    table = VisualSpec(type="table", source="ventes", x="famille", total_row=True,
                       columns=[ColumnSpec(key="ventes_tot", total_measure="fantome")])
    with pytest.raises(SpecError, match="total_measure"):
        apply_ops(d, [AddVisualOp(visual=table)])
