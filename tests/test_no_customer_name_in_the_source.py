"""No customer's name anywhere in this public repository.

`test_no_hardcoded_identities.py` guards e-mail addresses. It does not guard
the bare company name in a path, an identifier, a comment or a test fixture.
A customer's code and tests live in that customer's own overlay, never here:
whatever is committed to this repository is public for good, and `src/` is
also shipped in every release.

The names are held as SHA-256 digests of the lower-cased word. A guard that
spells a customer's name in a public repository reintroduces the very string
it exists to remove. To add one:

    python -c "import hashlib; print(hashlib.sha256(b'name').hexdigest())"

Every tracked file is read, path included, and split into words, identifiers
too: ``fooBar_baz88`` and ``FOOBar`` yield their words.

One occurrence is frozen rather than fixed, and deliberately so: a scheduled
job outside this repository reads that path, and moving it would orphan the
files already written. That is an operations change, not a rewording. The set
may shrink; a new entry means a name went back into the repository.
"""

from __future__ import annotations

import hashlib
import pathlib
import re
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

CUSTOMER_NAME_DIGESTS = {
    "05d418fee5ccf2a01a0c339e7859bdc9ebb6a5b8401c1320ba8b76958260ee76",
}
WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")

# Frozen on 2026-08-31. May shrink, never grow.
_DEBT = {"src/apowerb/storage/webhook_attachments.py"}


def _names_a_customer(text: str) -> bool:
    words = {word.lower() for word in WORD.findall(text)}
    return any(
        hashlib.sha256(word.encode()).hexdigest() in CUSTOMER_NAME_DIGESTS
        for word in words
    )


def _tracked_files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [path for path in listed.split("\0") if path]


def _files_naming_a_customer() -> set[str]:
    named = set()
    for relative in _tracked_files():
        path = REPO_ROOT / relative
        body = path.read_text(errors="ignore") if path.is_file() else ""
        if _names_a_customer(relative) or _names_a_customer(body):
            named.add(relative)
    return named


def test_the_check_recognises_a_guarded_name(monkeypatch):
    """Positive control, on a stand-in name: an identifier, a camel-cased
    class, a domain and a path built on it are all caught."""
    acme = hashlib.sha256(b"acme").hexdigest()
    assert not _names_a_customer("AcmeIntakePayload")
    monkeypatch.setitem(globals(), "CUSTOMER_NAME_DIGESTS", {acme})
    for text in (
        "acme_ar_assistant",
        "AcmeIntakePayload",
        "ACMEIntakePayload",
        "ops@sub.Acme88.fr",
        "tests/unit_acme/test_gate.py",
    ):
        assert _names_a_customer(text), text
    assert not _names_a_customer("academic macme acmes")


def test_the_frozen_entry_still_carries_one():
    """The debt list must describe reality. A file that no longer holds a name
    should leave the set rather than sit in it looking like an excuse."""
    stale = _DEBT - _files_naming_a_customer()
    assert not stale, f"to remove from _DEBT: {sorted(stale)}"


def test_no_tracked_file_names_a_customer():
    offenders = sorted(_files_naming_a_customer() - _DEBT)
    assert not offenders, (
        "a customer's name is committed to a public repository. Its code and "
        "tests belong in that customer's overlay; use a neutral name such as "
        "acme / acme.example here:\n  " + "\n  ".join(offenders)
    )
