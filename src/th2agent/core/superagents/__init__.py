"""SuperAgents registry — preconfigured agent templates.

Each template defines a full agent configuration that pre-fills the creation form.
Templates are defined in code and served read-only via the API.

The package splits the original monolithic module into per-family submodules
(data, marketing, rag, image, audio, dashboard) and re-exports the public
surface (``SUPERAGENT_TEMPLATES``, ``list_superagent_templates``,
``get_superagent_template``) for backward-compatible imports.

Per-org visibility
------------------
Templates may set ``visible_to_orgs: list[str]`` to restrict who sees them.
``None`` (or missing) means visible to all authenticated users — the default
behaviour for every legacy template. Org slugs are derived from the
user's email domain via the generic ``settings.org_domain_slugs`` mapping
(empty by default — see ``_user_org_slugs``).

A caller passing ``user=None`` opts out of visibility filtering — used by
internal code paths (e.g. boot-time inventories) that pre-date the auth
layer. Routes serving the UI must always pass the authenticated user.
"""

import hashlib
import json
from typing import TYPE_CHECKING, Iterable

from th2agent.configs.settings import get_settings
from th2agent.core.superagents.templates import SUPERAGENT_TEMPLATES, _build_templates


# Fields that participate in the template "version" — when any of them
# changes, agents created from this template are considered out-of-sync
# and the UI surfaces a "template updated" banner. Cosmetic fields
# (description, name) and user-overridable runtime knobs (model,
# model_params, memory_enabled, artifacts_enabled, visible_to_orgs)
# are intentionally excluded.
_TEMPLATE_HASH_FIELDS: tuple[str, ...] = (
    "agent_instruction",
    "agent_tools",
    "tags",
    # v2 sub-agent pipeline fields (PR #174 + #176): drift on these
    # must trigger the UI banner so operators know to resync.
    "output_key",
    "output_schema_name",
    "skip_when_upstream",
)
# Fields that ``resync_agent_to_template`` overwrites. Strict subset of
# ``_TEMPLATE_HASH_FIELDS``: the UI promises to only touch the agent's
# instruction & tool list, leaving credentials / model / customisations
# untouched.
_TEMPLATE_RESYNC_FIELDS: tuple[str, ...] = (
    "agent_instruction",
    "agent_tools",
    "tags",
    # v2 sub-agent pipeline fields (PR #174 + #176): when a template
    # changes its output_key / output_schema_name / skip_when_upstream
    # (e.g. typo fix), the resync endpoint must propagate it to the
    # instantiated agent — otherwise the agent stays on the stale
    # config and the pipeline silently drifts.
    "output_key",
    "output_schema_name",
    "skip_when_upstream",
)

if TYPE_CHECKING:
    from th2agent.users import schemas as user_schemas


def _user_org_slugs(user: "user_schemas.User | None") -> set[str]:
    """Return the org slugs the *user* belongs to.

    Org membership is derived from the email domain mapping configured
    under ``settings.org_domain_slugs`` (env var ``ORG_DOMAIN_SLUGS``,
    e.g. ``{"example.com": "example"}``). Empty by default — the rest
    of the visibility plumbing is org-agnostic.
    """
    if user is None:
        return set()
    settings = get_settings()
    email = (getattr(user, "email", "") or "").lower()
    slugs: set[str] = set()
    for domain, slug in (settings.org_domain_slugs or {}).items():
        if email.endswith(f"@{domain.lower()}"):
            slugs.add(slug)
    return slugs


def _is_visible_to(template: dict, org_slugs: Iterable[str]) -> bool:
    """Return True iff *template* should be served to a caller belonging
    to any of *org_slugs*."""
    visibility = template.get("visible_to_orgs")
    if visibility is None:
        return True
    return any(slug in visibility for slug in org_slugs)


def list_superagent_templates(
    user: "user_schemas.User | None" = None,
) -> list[dict]:
    """Return all SuperAgent templates visible to *user*.

    When *user* is ``None`` (legacy / system call path), no visibility
    filter is applied — the full list is returned. UI routes must always
    pass the authenticated user.
    """
    if user is None:
        return list(_build_templates())
    org_slugs = _user_org_slugs(user)
    return [t for t in _build_templates() if _is_visible_to(t, org_slugs)]


def get_superagent_template(
    template_id: str,
    user: "user_schemas.User | None" = None,
) -> dict | None:
    """Return a single SuperAgent template by id, or ``None`` if not
    visible to *user*. ``user=None`` skips the visibility check."""
    org_slugs = None if user is None else _user_org_slugs(user)
    for t in _build_templates():
        if t["template_id"] != template_id:
            continue
        if org_slugs is None or _is_visible_to(t, org_slugs):
            return t
    return None


def _canonical_hash_payload(template: dict) -> bytes:
    """Build a deterministic byte payload from the hash-relevant template
    fields, so the hash is stable across Python runs and dict orderings."""
    snapshot = {}
    for field in _TEMPLATE_HASH_FIELDS:
        value = template.get(field)
        # Lists are compared positionally — agent_tools order matters
        # (it drives priority in load_agent_tools_functions). Tags too:
        # we don't sort, otherwise re-ordering tags wouldn't bump the hash
        # while still being a real change to the prompt's "tags:" line.
        snapshot[field] = value
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")


def compute_template_hash(template_id: str) -> str | None:
    """Return a stable SHA-256 of the template's hash-relevant fields, or
    ``None`` if the template is unknown.

    Used to detect drift between an agent's stored snapshot of its template
    (``th2agents_store.superagent_template_version_hash``) and the current
    in-code template. When they diverge, the UI surfaces a "template
    updated, click to sync" banner and ``resync_agent_to_template``
    overwrites the agent's hash-relevant fields with the live template
    values.
    """
    template = get_superagent_template(template_id, user=None)
    if template is None:
        return None
    return hashlib.sha256(_canonical_hash_payload(template)).hexdigest()


def diff_agent_against_template(
    agent: dict,
    template_id: str,
) -> dict:
    """Return a structured drift report for ``agent`` against its template.

    Result shape::

        {
            "template_id":       "some_template_id",
            "is_in_sync":        False,
            "stored_hash":       "abc123…",  # what the agent was created with
            "current_hash":      "def456…",  # what the template says now
            "drift_fields":      ["agent_instruction", "agent_tools"],
        }

    ``agent`` is expected to expose the same keys as the rows returned by
    ``get_agent`` (``agent_instruction``, ``agent_tools`` (already parsed
    list), ``tags`` (already parsed list),
    ``superagent_template_version_hash``). Returns ``is_in_sync=True`` and
    an empty ``drift_fields`` when the template is unknown — the caller
    decides whether to surface that as an error.
    """
    template = get_superagent_template(template_id, user=None)
    stored_hash = agent.get("superagent_template_version_hash")
    if template is None:
        return {
            "template_id": template_id,
            "is_in_sync": True,
            "stored_hash": stored_hash,
            "current_hash": None,
            "drift_fields": [],
            "template_unknown": True,
        }

    current_hash = hashlib.sha256(_canonical_hash_payload(template)).hexdigest()
    drift_fields: list[str] = []
    for field in _TEMPLATE_HASH_FIELDS:
        agent_value = agent.get(field)
        template_value = template.get(field)
        # ``tool_config*`` entries on the agent are user-owned attachments
        # (DB credentials, OAuth tool configs) — they are never present in
        # the template. Comparing them as-is would flag every agent with
        # any tool_config attached as out-of-sync forever. Strip them out
        # before the comparison so we only diff the native (template)
        # entries.
        if field == "agent_tools" and isinstance(agent_value, list):
            agent_value = [
                t for t in agent_value
                if not (isinstance(t, str) and t.startswith("tool_config"))
            ]
        if agent_value != template_value:
            drift_fields.append(field)

    return {
        "template_id": template_id,
        "is_in_sync": current_hash == stored_hash and not drift_fields,
        "stored_hash": stored_hash,
        "current_hash": current_hash,
        "drift_fields": drift_fields,
    }


__all__ = [
    "SUPERAGENT_TEMPLATES",
    "list_superagent_templates",
    "get_superagent_template",
    "compute_template_hash",
    "diff_agent_against_template",
    "_TEMPLATE_HASH_FIELDS",
    "_TEMPLATE_RESYNC_FIELDS",
]
