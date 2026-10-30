"""Tests for the filename sanitizer contract in storage/filename.py."""

import pytest

from apowerb.storage.filename import sanitize_filename


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Empty / missing fall back to the default name.
        (None, "attachment"),
        ("", "attachment"),
        # Path components are stripped (basename).
        ("../../etc/passwd", "passwd"),
        ("foo/bar/baz.txt", "baz.txt"),
        ("/absolute/path/file.pdf", "file.pdf"),
        ("file/name.txt", "name.txt"),
        # Clean names pass through unchanged, including unicode letters.
        ("report-2026.pdf", "report-2026.pdf"),
        ("résumé.pdf", "résumé.pdf"),
        ("my file.txt", "my file.txt"),
        # NUL and other non-printable characters are removed.
        ("file\x00name.txt", "filename.txt"),
        ("file\nname.txt", "filename.txt"),
        ("file\tname.txt", "filename.txt"),
        # Literal ".." is neutralized.
        ("file..name.txt", "file_name.txt"),
        ("..hidden", "_hidden"),
        # Disallowed characters become underscores.
        ("file#name%*.txt", "file_name__.txt"),
        ("file\\name.txt", "file_name.txt"),
        # Leading dots are stripped (never a dotfile).
        (".hidden", "hidden"),
        # Trailing dots and spaces are stripped.
        ("name. ", "name"),
        ("name.", "name"),
        ("name .txt ", "name .txt"),
        # Result never empty.
        (".", "attachment"),
        ("   ", "attachment"),
        ("...", "_"),
    ],
)
def test_sanitize_filename_contract(raw: str | None, expected: str) -> None:
    assert sanitize_filename(raw) == expected
