"""Every template module built as DESCRIPTOR + build is listed by the catalogue."""

import pytest

from apowerb.bi.spec import catalog

NEW_TEMPLATES = ["delivery_otif", "production_quality", "sales_tracking", "sales_forecast"]


@pytest.mark.parametrize("template_id", NEW_TEMPLATES)
def test_template_is_registered_with_its_descriptor(template_id):
    template = catalog.get_template(template_id)
    assert template is not None
    described = template.describe()
    assert described["origin"] == "builtin" and described["version"] >= 1
    assert any(c["required"] for c in described["columns"])
    assert template_id in [t.id for t in catalog.list_templates()]


@pytest.mark.parametrize("template_id", NEW_TEMPLATES)
def test_registered_template_builds_a_valid_spec_with_its_defaults(template_id):
    template = catalog.get_template(template_id)
    columns, errors = template.clean_columns({c.key: c.default for c in template.columns})
    params, param_errors = template.clean_params({})
    assert errors == [] and param_errors == []
    spec = template.build("ds-1", columns, params)
    assert spec["sources"] and spec["components"]


def test_stock_template_is_listed_first():
    assert catalog.list_templates()[0].id == "stock_pilotage"
