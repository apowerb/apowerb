"""The permission catalogue.

Declared here rather than free-form strings in the database: a permission
that no code reads is worse than no permission at all — it reads as a
granted capability while enforcing nothing. Adding one to this list is the
deliberate act of introducing it.

Names mirror the product's own surfaces so a reader can map a checkbox to
what it opens.
"""

from __future__ import annotations

PERMISSIONS: tuple[dict[str, str], ...] = (
    {"name": "agents.read", "label": "View agents"},
    {"name": "agents.write", "label": "Create and edit agents"},
    {"name": "evaluations.run", "label": "Run evaluations"},
    {"name": "bi.read", "label": "View BI and reports"},
    {"name": "integrations.manage", "label": "Manage integrations"},
    {"name": "webhooks.manage", "label": "Manage webhooks"},
    {"name": "usage.read", "label": "View usage and quotas"},
    {"name": "admin.manage", "label": "Administer users, groups and permissions"},
)

KNOWN = frozenset(p["name"] for p in PERMISSIONS)


def unknown_permissions(names: list[str]) -> list[str]:
    """Which of `names` this build cannot enforce.

    Returned rather than raised so the caller decides the status code, and
    so the message can name every offender at once instead of the first.
    """
    return sorted(set(names) - KNOWN)
