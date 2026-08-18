"""A permission the code cannot enforce must never be storable.

Granting one would display as a capability while enforcing nothing — the
worst of both, because the screen then lies about what the group can do.
"""

from apowerb.admin.permissions import KNOWN, PERMISSIONS, unknown_permissions


def test_the_catalogue_is_self_consistent():
    assert KNOWN == {p["name"] for p in PERMISSIONS}
    assert all(p["label"] for p in PERMISSIONS), "every permission needs a label to render"


def test_unknown_permissions_are_all_reported_not_just_the_first():
    """Naming one offender at a time turns fixing a payload into a loop."""
    assert unknown_permissions(["agents.read", "nope.one", "nope.two"]) == [
        "nope.one", "nope.two",
    ]


def test_a_fully_valid_list_reports_nothing():
    assert unknown_permissions(sorted(KNOWN)) == []
