"""Containment: a built path must resolve back inside the base it was joined under.

These pin the behaviour the format guards elsewhere cannot provide. A symlink
named `agent12` passes every `[A-Za-z0-9_-]` check ever written and still points
at /etc -- validating the shape says nothing about the destination.
"""
from __future__ import annotations

import os

import pytest

from apowerb.helpers.safe_paths import PathEscape, contained_path


@pytest.fixture()
def base(tmp_path):
    d = tmp_path / "store"
    d.mkdir()
    return d


def test_it_joins_under_the_base(base):
    assert contained_path(base, "agent12", "notes.txt") == str(
        base.resolve() / "agent12" / "notes.txt"
    )


def test_a_single_component_is_allowed(base):
    assert contained_path(base, "agent12") == str(base.resolve() / "agent12")


def test_many_components_nest(base):
    got = contained_path(base, "users", "u1", "sessions", "s1", "artifacts")
    assert got == str(base.resolve() / "users" / "u1" / "sessions" / "s1" / "artifacts")


def test_it_accepts_a_path_object_as_base(base):
    assert contained_path(base, "x") == contained_path(str(base), "x")


def test_non_string_components_are_coerced(base):
    """Callers pass ints (agent ids, version numbers) without str() at the site."""
    assert contained_path(base, 12, "notes.txt") == str(
        base.resolve() / "12" / "notes.txt"
    )


@pytest.mark.parametrize(
    "parts",
    [
        ("..",),
        (".",),
        ("..", "..", "etc"),
        ("agent12", "..", "..", "etc", "passwd"),
        ("/etc/passwd",),
        ("agent12", "/etc/passwd"),
        ("../../../../../../etc/passwd",),
    ],
)
def test_traversal_and_absolute_components_are_rejected(base, parts):
    with pytest.raises(PathEscape):
        contained_path(base, *parts)


def test_the_base_itself_is_not_a_valid_target(base):
    with pytest.raises(PathEscape):
        contained_path(base, ".")


def test_a_sibling_sharing_the_prefix_is_rejected(base):
    """`startswith(base + os.sep)`, not `startswith(base)`.

    Without the separator, `store-evil` shares the prefix of `store` and passes.
    """
    (base.parent / "store-evil").mkdir()
    with pytest.raises(PathEscape):
        contained_path(base, "..", "store-evil", "loot.txt")


def test_a_symlink_pointing_out_of_the_base_is_rejected(base, tmp_path):
    """The case no format guard can catch: a legal name, an illegal target."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s3cret")
    (base / "agent12").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathEscape):
        contained_path(base, "agent12", "secret.txt")


def test_a_symlink_staying_inside_the_base_is_allowed(base):
    """Containment, not a symlink prohibition."""
    real = base / "agent12"
    real.mkdir()
    (base / "alias").symlink_to(real, target_is_directory=True)

    assert contained_path(base, "alias", "notes.txt") == str(
        real.resolve() / "notes.txt"
    )


def test_it_returns_a_normalised_absolute_path(base):
    got = contained_path(base, "agent12", "sub", "..", "notes.txt")
    assert got == str(base.resolve() / "agent12" / "notes.txt")
    assert os.path.isabs(got)


def test_the_error_is_a_valueerror_not_an_http_exception():
    """Used from the agent runtime as well as from routers.

    A 400 raised outside a request would misreport what went wrong; routers
    translate PathEscape at their boundary instead.
    """
    assert issubclass(PathEscape, ValueError)
