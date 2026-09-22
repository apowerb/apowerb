"""Validation des 8 kinds de trigger (contrat T1) — cas valides et contre-exemples.

``validate_trigger_config`` ne valide que la FORME : les kinds T2 (email,
agent_tool, form, file, workflow_done) sont acceptés ici s'ils sont bien
formés — c'est l'API de gestion qui répond ``active:false,
reason:"not_available"`` pour eux tant qu'ils ne sont pas exécutés.
"""

import pytest

from apowerb.core import workflow_graph as wg


def _code(exc_info) -> str:
    return exc_info.value.code


def _cfg(kind, **extra):
    return {"kind": kind, **extra}


# --- kind inconnu -------------------------------------------------------------


def test_manual_is_the_default_and_always_valid():
    wg.validate_trigger_config({})
    wg.validate_trigger_config({"kind": "manual"})


def test_unknown_kind_is_refused():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config({"kind": "carrier_pigeon"})
    assert _code(exc) == "unknown_trigger_kind"


# --- webhook -------------------------------------------------------------


def test_webhook_default_hmac_false_is_valid():
    wg.validate_trigger_config(_cfg("webhook"))
    wg.validate_trigger_config(_cfg("webhook", hmac=True))


def test_webhook_hmac_must_be_boolean():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(_cfg("webhook", hmac="yes"))
    assert _code(exc) == "invalid_field"


# --- schedule --------------------------------------------------------------


def test_schedule_needs_exactly_one_of_cron_or_at():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(_cfg("schedule"))
    assert _code(exc) == "schedule_needs_one_of_cron_or_at"

    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg("schedule", cron="0 * * * *", at="2099-01-01T00:00:00+00:00")
        )
    assert _code(exc) == "schedule_needs_one_of_cron_or_at"


def test_schedule_valid_cron_every_five_minutes():
    wg.validate_trigger_config(_cfg("schedule", cron="*/5 * * * *"))
    wg.validate_trigger_config(
        _cfg("schedule", cron="0 9 * * 1-5", timezone="Europe/Paris")
    )


@pytest.mark.parametrize(
    "cron", ["* * * * *", "*/1 * * * *", "0,1 * * * *", "58,2 * * * *"]
)
def test_schedule_cron_too_frequent_is_refused(cron):
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(_cfg("schedule", cron=cron))
    assert _code(exc) == "cron_too_frequent"


def test_schedule_cron_malformed_is_refused():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(_cfg("schedule", cron="0 * * *"))  # 4 champs
    assert _code(exc) == "invalid_cron"

    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg("schedule", cron="99 * * * *")
        )  # minute hors bornes
    assert _code(exc) == "invalid_cron"


def test_schedule_unknown_timezone_is_refused():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg("schedule", cron="0 * * * *", timezone="Mars/OlympusMons")
        )
    assert _code(exc) == "unknown_timezone"


def test_schedule_at_in_the_past_is_refused():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(_cfg("schedule", at="2000-01-01T00:00:00+00:00"))
    assert _code(exc) == "at_in_past"


def test_schedule_at_in_the_future_is_valid():
    wg.validate_trigger_config(_cfg("schedule", at="2099-01-01T00:00:00+00:00"))


# --- email (T2, forme seulement) --------------------------------------------


def test_email_valid_provider():
    wg.validate_trigger_config(_cfg("email", provider="outlook"))
    wg.validate_trigger_config(
        _cfg("email", provider="gmail", from_filter="boss@corp.com")
    )


def test_email_unknown_provider_is_refused():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(_cfg("email", provider="carrier_pigeon"))
    assert _code(exc) == "unknown_email_provider"


# --- agent_tool (T2, forme seulement) ---------------------------------------


def test_agent_tool_valid():
    wg.validate_trigger_config(
        _cfg(
            "agent_tool",
            tool_name="lookup_order",
            description="Cherche une commande",
            input_schema=[{"name": "order_id", "type": "string", "required": True}],
        )
    )


def test_agent_tool_name_must_match_slug():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg(
                "agent_tool", tool_name="Not A Slug!", description="x", input_schema=[]
            )
        )
    assert _code(exc) == "invalid_tool_name"


def test_agent_tool_description_required():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg("agent_tool", tool_name="lookup", input_schema=[])
        )
    assert _code(exc) == "tool_description_required"


def test_agent_tool_duplicate_field_is_refused():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg(
                "agent_tool",
                tool_name="lookup",
                description="x",
                input_schema=[
                    {"name": "id", "type": "string"},
                    {"name": "id", "type": "number"},
                ],
            )
        )
    assert _code(exc) == "duplicate_tool_field"


def test_agent_tool_unknown_field_type_is_refused():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg(
                "agent_tool",
                tool_name="lookup",
                description="x",
                input_schema=[{"name": "id", "type": "datetime"}],
            )
        )
    assert _code(exc) == "invalid_input_schema_type"


# --- form (T2, forme seulement) ---------------------------------------------


def test_form_valid():
    wg.validate_trigger_config(
        _cfg(
            "form",
            title="Contact",
            fields=[
                {"name": "email", "label": "E-mail", "type": "text", "required": True}
            ],
            access="public",
        )
    )


def test_form_requires_at_least_one_field():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg("form", title="Contact", fields=[], access="public")
        )
    assert _code(exc) == "form_fields_required"


def test_form_duplicate_field_name_is_refused():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg(
                "form",
                title="Contact",
                fields=[
                    {"name": "email", "label": "E-mail", "type": "text"},
                    {"name": "email", "label": "E-mail bis", "type": "text"},
                ],
                access="public",
            )
        )
    assert _code(exc) == "duplicate_form_field"


def test_form_unknown_access_is_refused():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg(
                "form",
                title="Contact",
                fields=[{"name": "email", "label": "E-mail", "type": "text"}],
                access="anyone",
            )
        )
    assert _code(exc) == "invalid_form_access"


# --- file (T2, forme seulement) ---------------------------------------------


def test_file_valid_default_interval():
    wg.validate_trigger_config(
        _cfg("file", provider="onedrive", folder_id="F1", folder_label="Factures")
    )


def test_file_unknown_provider_is_refused():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg("file", provider="dropbox", folder_id="F1", folder_label="Factures")
        )
    assert _code(exc) == "unknown_file_provider"


@pytest.mark.parametrize("interval", [0, 4, 1441, -5])
def test_file_interval_out_of_bounds_is_refused(interval):
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg(
                "file",
                provider="google_drive",
                folder_id="F1",
                folder_label="Factures",
                interval_min=interval,
            )
        )
    assert _code(exc) == "invalid_file_interval"


# --- workflow_done (T2, forme seulement, mais l'auto-écoute est testable) ---


def test_workflow_done_valid():
    wg.validate_trigger_config(
        _cfg("workflow_done", workflow_id="wf-other", on="success")
    )


def test_workflow_done_unknown_on_is_refused():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg("workflow_done", workflow_id="wf-other", on="maybe")
        )
    assert _code(exc) == "invalid_workflow_done_on"


def test_workflow_done_self_listen_is_refused_when_workflow_id_known():
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(
            _cfg("workflow_done", workflow_id="wf-self", on="any"),
            workflow_id="wf-self",
        )
    assert _code(exc) == "workflow_done_self_listen"


def test_workflow_done_self_listen_not_checked_without_workflow_id():
    # Sans identifiant connu (ex. validation avant création) : pas de refus.
    wg.validate_trigger_config(_cfg("workflow_done", workflow_id="wf-self", on="any"))


# --- intégration avec validate_graph (le trigger est un nœud du graphe) ----


def _graph(trigger_config):
    return wg.WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [{"id": "start", "type": "trigger", "config": trigger_config}],
            "edges": [],
        }
    )


def test_validate_graph_rejects_an_invalid_trigger_kind():
    with pytest.raises(wg.GraphError, match="kind de trigger inconnu"):
        wg.validate_graph(_graph({"kind": "nope"}))


def test_validate_graph_accepts_a_well_formed_webhook_trigger():
    wg.validate_graph(_graph({"kind": "webhook", "hmac": True}))


def test_validate_graph_threads_workflow_id_for_self_listen():
    with pytest.raises(wg.GraphError, match="s'écouter lui-même"):
        wg.validate_graph(
            _graph({"kind": "workflow_done", "workflow_id": "wf-x", "on": "any"}),
            workflow_id="wf-x",
        )


def test_trigger_spec_reads_the_head_trigger_node_kind():
    assert wg.trigger_spec(_graph({})) == {"kind": "manual"}
    assert wg.trigger_spec(_graph({"kind": "webhook", "hmac": True})) == {
        "kind": "webhook",
        "hmac": True,
    }


@pytest.mark.parametrize("flt", ["acme", "boss@", "@", "a b@c.fr", "@acme"])
def test_email_from_filter_must_be_an_address_or_a_domain(flt):
    with pytest.raises(wg.GraphError) as exc:
        wg.validate_trigger_config(_cfg("email", provider="gmail", from_filter=flt))
    assert exc.value.code == "invalid_field"


@pytest.mark.parametrize("flt", ["boss@corp.com", "@corp.com", "corp.com", ""])
def test_email_from_filter_accepts_an_address_or_a_domain(flt):
    wg.validate_trigger_config(_cfg("email", provider="gmail", from_filter=flt))
