"""Containment: every uploads path must resolve back inside uploads_dir().

The format guards (`_safe_agent_id`, `_validate_upload_id`) reject the shapes
we can name, but they cannot see the filesystem. A symlink planted inside
uploads_dir() has a perfectly legal name and still points elsewhere, so a
rejected *shape* is not a proven *location* -- these tests pin the location.

They also pin the one construction CodeQL's py/path-injection accepts:
normalisation (`os.path.realpath`) followed by a `.startswith()` check on the
branch that returns. Two revisions of a strict regex guard were reported
anyway; this is not a stylistic preference, it is the analysed contract.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from apowerb.routers import files as files_router
from apowerb.routers.files import _contained_upload_path


@pytest.fixture()
def uploads(tmp_path, monkeypatch):
    base = tmp_path / "uploads"
    base.mkdir()
    monkeypatch.setattr(files_router, "uploads_dir", lambda: base)
    return base


def test_it_joins_under_the_base(uploads):
    got = _contained_upload_path("agent12", "notes.txt")
    assert got == str(uploads.resolve() / "agent12" / "notes.txt")


def test_a_directory_only_join_is_allowed(uploads):
    assert _contained_upload_path("agent12") == str(uploads.resolve() / "agent12")


@pytest.mark.parametrize(
    "parts",
    [
        ("..",),
        ("..", "..", "etc"),
        ("agent12", "..", "..", "etc", "passwd"),
        ("/etc/passwd",),
        ("agent12", "/etc/passwd"),
    ],
)
def test_traversal_lands_outside_and_is_rejected(uploads, parts):
    with pytest.raises(HTTPException) as exc:
        _contained_upload_path(*parts)
    assert exc.value.status_code == 400


def test_the_base_itself_is_not_a_valid_target(uploads):
    """`startswith(base + os.sep)` -- not `startswith(base)`.

    Without the separator, a sibling directory named `uploads-evil` shares the
    prefix and passes.
    """
    with pytest.raises(HTTPException):
        _contained_upload_path(".")


def test_a_sibling_sharing_the_prefix_is_rejected(uploads):
    sibling = uploads.parent / "uploads-evil"
    sibling.mkdir()
    with pytest.raises(HTTPException):
        _contained_upload_path("..", "uploads-evil", "loot.txt")


def test_a_symlink_pointing_out_of_the_base_is_rejected(uploads, tmp_path):
    """The case no format guard can catch: a legal name, an illegal target."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s3cret")

    (uploads / "agent12").symlink_to(outside, target_is_directory=True)

    with pytest.raises(HTTPException) as exc:
        _contained_upload_path("agent12", "secret.txt")
    assert exc.value.status_code == 400


def test_a_symlink_staying_inside_the_base_is_allowed(uploads):
    real = uploads / "agent12"
    real.mkdir()
    (uploads / "alias").symlink_to(real, target_is_directory=True)

    got = _contained_upload_path("alias", "notes.txt")
    assert got == str(real.resolve() / "notes.txt")


def test_it_returns_a_normalised_absolute_path(uploads):
    got = _contained_upload_path("agent12", "sub", "..", "notes.txt")
    assert got == str(uploads.resolve() / "agent12" / "notes.txt")
    assert os.path.isabs(got)
