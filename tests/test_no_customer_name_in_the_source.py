"""No customer's name in the source this package publishes.

`test_no_hardcoded_identities.py` guards e-mail addresses. It does not guard
the bare company name in a comment, and sixty-nine lines across twenty-five
files carried one -- every release shipping them into everyone's
site-packages, since `src/` is what gets packaged.

The forbidden words are derived from `INTERNAL_DOMAINS` in that other guard
rather than written out again here. Two reasons. A guard that has to spell a
customer's name in a public repository reintroduces the very string it exists
to remove; and deriving means that adding a customer domain there extends this
check for free, instead of leaving it to be remembered.

One occurrence is frozen rather than fixed, and deliberately so: a scheduled
job outside this repository reads that path, and moving it would orphan the
files already written. That is an operations change, not a rewording. The set
may shrink; a new entry means a name went back into the source.
"""

from __future__ import annotations

import pathlib
import re

import apowerb
from tests.test_no_hardcoded_identities import INTERNAL_DOMAINS

# The company part of each guarded domain: "example88.fr" -> "example".
FORBIDDEN = {
    m.group(0)
    for domain in INTERNAL_DOMAINS
    for m in [re.match(r"[a-z]+", domain)]
    if m and len(m.group(0)) >= 4
}
# `INTERNAL_DOMAINS` holds "ours and our customers'" together. Only the
# customers' side belongs here: our own name is legitimate in this source, and
# the shared-model provider id `thaink2/default` is a public identifier the
# other guard already allows by name.
OURS = {"thaink", "th2ai"}
FORBIDDEN -= OURS

# Frozen on 2026-08-31. May shrink, never grow.
_SOURCE_DEBT = {"apowerb/storage/webhook_attachments.py"}


def _source_files():
    root = pathlib.Path(apowerb.__file__).parent
    for path in sorted(root.rglob("*.py")):
        yield path.relative_to(root.parent), path.read_text()


def test_the_forbidden_list_is_not_empty():
    """Positive control on the derivation. Were `INTERNAL_DOMAINS` to change
    shape, this check would quietly start guarding nothing."""
    assert FORBIDDEN
    assert all(word.isalpha() for word in FORBIDDEN), FORBIDDEN


def test_the_frozen_entry_still_carries_one():
    """The debt list must describe reality. A file that no longer holds a name
    should leave the set rather than sit in it looking like an excuse."""
    stale = set()
    for rel, body in _source_files():
        if str(rel) in _SOURCE_DEBT and not any(w in body.lower() for w in FORBIDDEN):
            stale.add(str(rel))

    assert not stale, f"a retirer de _SOURCE_DEBT : {stale}"


def test_no_source_file_names_a_customer():
    """The sweep. `src/` is what `uv_build` packages, so anything here is
    published with every release and cannot be taken back."""
    offenders = []
    for rel, body in _source_files():
        if str(rel) in _SOURCE_DEBT:
            continue
        for i, line in enumerate(body.splitlines(), 1):
            low = line.lower()
            hit = [w for w in FORBIDDEN if w in low]
            if hit:
                offenders.append(f"{rel}:{i}")

    assert offenders == []
