"""Tests for the template-drift detection helpers (PR #119).

Covers ``compute_template_hash`` and ``diff_agent_against_template`` —
the two functions the new ``/agents/{id}/template-status`` and
``/agents/{id}/resync-template`` endpoints sit on top of. The endpoints
themselves are exercised in integration suites that hit a running
backend; here we test the pure logic without spinning up FastAPI.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from apowerb.core.superagents import (
    _TEMPLATE_HASH_FIELDS,
    _TEMPLATE_RESYNC_FIELDS,
    compute_template_hash,
    diff_agent_against_template,
    get_superagent_template,
)


# --------------------------------------------------------------------------- #
# compute_template_hash
# --------------------------------------------------------------------------- #


def test_unknown_template_returns_none():
    assert compute_template_hash("does_not_exist_xyz") is None


def test_hash_is_stable_across_calls():
    """Same template → same hash. The frontend polls /template-status on
    every page load; nondeterministic hashes would make every refresh look
    like a drift."""
    h1 = compute_template_hash("scei_ar_assistant")
    h2 = compute_template_hash("scei_ar_assistant")
    assert h1 is not None
    assert h1 == h2


def test_hash_is_64_hex_chars_sha256():
    h = compute_template_hash("scei_ar_assistant")
    assert h is not None
    assert len(h) == 64
    int(h, 16)  # raises ValueError if not hex


def test_hash_changes_when_instruction_changes():
    """Mutating ``agent_instruction`` must bump the hash so an existing
    agent is flagged as out-of-sync."""
    original = compute_template_hash("scei_ar_assistant")

    # Patch the in-memory list ; module under test re-reads it on each call.
    from apowerb.core.superagents import templates as tpl_mod

    edited = []
    for t in tpl_mod.SUPERAGENT_TEMPLATES:
        if t["template_id"] == "scei_ar_assistant":
            t = {**t, "agent_instruction": t["agent_instruction"] + "\n## addendum\n"}
        edited.append(t)

    with patch.object(tpl_mod, "SUPERAGENT_TEMPLATES", edited):
        # The package re-exports the list via __init__; patch that too.
        from apowerb.core import superagents as pkg

        with patch.object(pkg, "SUPERAGENT_TEMPLATES", edited):
            new_hash = compute_template_hash("scei_ar_assistant")

    assert new_hash != original


def test_hash_changes_when_tools_reordered():
    """Order of ``agent_tools`` matters (drives priority in
    load_agent_tools_functions). Reordering must surface as drift."""
    from apowerb.core import superagents as pkg
    from apowerb.core.superagents import templates as tpl_mod

    original = compute_template_hash("scei_ar_assistant")

    edited = []
    for t in tpl_mod.SUPERAGENT_TEMPLATES:
        if t["template_id"] == "scei_ar_assistant":
            t = {**t, "agent_tools": list(reversed(t["agent_tools"]))}
        edited.append(t)

    with patch.object(tpl_mod, "SUPERAGENT_TEMPLATES", edited), \
         patch.object(pkg, "SUPERAGENT_TEMPLATES", edited):
        new_hash = compute_template_hash("scei_ar_assistant")

    assert new_hash != original


def test_hash_does_not_depend_on_excluded_fields():
    """Mutating a non-hash field (description, agent_name) must NOT change
    the hash — otherwise every cosmetic edit would force users to re-sync."""
    from apowerb.core import superagents as pkg
    from apowerb.core.superagents import templates as tpl_mod

    original = compute_template_hash("scei_ar_assistant")

    edited = []
    for t in tpl_mod.SUPERAGENT_TEMPLATES:
        if t["template_id"] == "scei_ar_assistant":
            t = {**t, "agent_description": "totally different description"}
        edited.append(t)

    with patch.object(tpl_mod, "SUPERAGENT_TEMPLATES", edited), \
         patch.object(pkg, "SUPERAGENT_TEMPLATES", edited):
        new_hash = compute_template_hash("scei_ar_assistant")

    assert new_hash == original


# --------------------------------------------------------------------------- #
# diff_agent_against_template
# --------------------------------------------------------------------------- #


def _agent_snapshot_of(template_id: str, **overrides) -> dict:
    """Build a minimal agent dict mirroring the template's hash fields.
    Tests then mutate via ``overrides`` to simulate drift."""
    tpl = get_superagent_template(template_id, user=None)
    assert tpl is not None
    snap = {field: tpl.get(field) for field in _TEMPLATE_HASH_FIELDS}
    snap["superagent_template_version_hash"] = compute_template_hash(template_id)
    snap.update(overrides)
    return snap


def test_in_sync_when_snapshot_matches():
    agent = _agent_snapshot_of("scei_ar_assistant")
    report = diff_agent_against_template(agent, "scei_ar_assistant")
    assert report["is_in_sync"] is True
    assert report["drift_fields"] == []


def test_drift_detected_when_instruction_changed():
    agent = _agent_snapshot_of(
        "scei_ar_assistant",
        agent_instruction="(stale instruction text)",
    )
    report = diff_agent_against_template(agent, "scei_ar_assistant")
    assert report["is_in_sync"] is False
    assert "agent_instruction" in report["drift_fields"]


def test_drift_detected_when_hash_was_never_stored():
    """Legacy agents created before PR #119 have stored_hash=None.
    They should show as out-of-sync so the UI nudges them to re-sync."""
    agent = _agent_snapshot_of(
        "scei_ar_assistant",
        superagent_template_version_hash=None,
    )
    # Even if the live fields happen to match, stored_hash=None and
    # current_hash!=None means "we can't prove sync" → out of sync.
    report = diff_agent_against_template(agent, "scei_ar_assistant")
    assert report["stored_hash"] is None
    assert report["current_hash"] is not None
    # is_in_sync requires hash equality; None != current_hash.
    assert report["is_in_sync"] is False


def test_unknown_template_returns_template_unknown_marker():
    agent = {"superagent_template_version_hash": "xxx"}
    report = diff_agent_against_template(agent, "phantom_template")
    assert report.get("template_unknown") is True
    assert report["is_in_sync"] is True  # nothing to compare against
    assert report["drift_fields"] == []


def test_user_tool_configs_ignored_in_diff():
    """``tool_config*`` entries are user attachments (DB creds, OAuth) — they
    are never in the template. Including them in the diff would flag every
    real-world agent as permanently out-of-sync. Live regression at 14:43
    UTC: agent6 lost its tool_config14/15 because resync wiped them."""
    agent = _agent_snapshot_of("scei_ar_assistant")
    # Simulate a real prod agent: same template tools + user-attached configs
    template_tools = list(agent["agent_tools"])
    agent["agent_tools"] = ["tool_config14", "tool_config15"] + template_tools

    report = diff_agent_against_template(agent, "scei_ar_assistant")
    assert "agent_tools" not in report["drift_fields"], (
        "Adding tool_config* entries must NOT be reported as drift — "
        "they are user-owned credentials, not template content."
    )
    assert report["is_in_sync"] is True


def test_drift_still_detected_when_native_tools_diverge_from_template():
    """tool_config* are filtered out of the diff, but a real divergence on
    a native tool (added/removed compared to the template) still surfaces."""
    agent = _agent_snapshot_of("scei_ar_assistant")
    agent["agent_tools"] = ["tool_config14"] + list(agent["agent_tools"])[:-1]
    # Dropped one native tool → drift on agent_tools should be reported
    report = diff_agent_against_template(agent, "scei_ar_assistant")
    assert "agent_tools" in report["drift_fields"]


# --------------------------------------------------------------------------- #
# resync_agent_to_template — preservation of user-owned tool_configs
# --------------------------------------------------------------------------- #


def test_resync_preserves_user_tool_configs(monkeypatch, tmp_path):
    """Live regression 2026-05-07 14:41 UTC: resync of agent6 dropped
    tool_config14 (PMI) + tool_config15 (SuiviAR), leaving the SCEI agent
    without any DB tool. These configs are user-owned credentials — they
    must survive a template resync."""
    from apowerb.core import agent_main
    from apowerb.core.agent_helpers import (  # noqa: F401  (imported for side effects)
        agent_utils,
    )

    # Simulate the prod agent6 row state before resync.
    captured_update: dict = {}
    original_agent_tools = [
        "tool_config14",                     # PMI (user attachment)
        "tool_config15",                     # SuiviAR (user attachment)
        "outlook_mail.tool_list_emails",     # template native
        "basic.tool_pdf_to_images",          # template native
    ]

    def fake_get_agent(agent_id, user_id):
        return {
            "agent_id": agent_id,
            "owner_id": user_id,
            "superagent_template_id": "scei_ar_assistant",
            "agent_instruction": "(stale)",
            "agent_tools": list(original_agent_tools),
            "agent_description": "x",
            "agent_model": "m",
        }

    class FakeConn:
        def execute(self, stmt):
            captured_update["values"] = stmt.compile().params
            return None

    class FakeEngineCtx:
        def __enter__(self):
            return FakeConn()
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(agent_main, "get_agent", fake_get_agent)
    monkeypatch.setattr(
        agent_main.agent_store.engine, "begin", lambda: FakeEngineCtx()
    )
    monkeypatch.setattr(
        agent_main, "create_agent_module", lambda **kw: None
    )
    # get_agent_template_status is called at the end — make it a no-op
    monkeypatch.setattr(
        agent_main, "get_agent_template_status",
        lambda agent_id, user_id: {"agent_id": agent_id, "is_in_sync": True},
    )

    agent_main.resync_agent_to_template(6, user_id="com@scei88.fr")

    # The UPDATE should have written agent_tools containing BOTH the user
    # tool_config* entries AND the template natives.
    new_tools = json.loads(captured_update["values"]["agent_tools"])
    assert "tool_config14" in new_tools, "tool_config14 dropped during resync"
    assert "tool_config15" in new_tools, "tool_config15 dropped during resync"
    # Template natives should also be present
    assert any(t.startswith("outlook_mail.") for t in new_tools)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #


def test_resync_fields_subset_of_hash_fields():
    """The UI promises that 'resync' only touches a subset of the
    drift-detection fields — we cannot suddenly start writing fields the
    drift report doesn't even watch."""
    assert set(_TEMPLATE_RESYNC_FIELDS).issubset(set(_TEMPLATE_HASH_FIELDS))
