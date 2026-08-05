"""Prove a built path stays inside the directory it was supposed to stay in.

Format guards answer "is this a plausible name?" -- they validate a *shape*.
They cannot answer "does this land where I think?", because that depends on the
filesystem: a symlink whose name matches `[A-Za-z0-9_-]` perfectly resolves
wherever it likes. A rejected shape is not a proven location, so the two checks
are complementary rather than alternatives, and this module covers the second.

It is also the only construction CodeQL's py/path-injection accepts as a
sanitiser. The query tracks two flow states and requires a normalisation call
(`os.path.normpath`, `abspath` or `realpath`) followed by a `.startswith()`
check on the branch that reaches the sink -- see PathInjectionQuery.qll and the
`OsPathRealpathCall` / `StartswithCall` models in Stdlib.qll. A regex guard is
not recognised, however strict; neither is `pathlib.Path.resolve()` with
`is_relative_to()`, nor `os.path.basename`. Three separate revisions of a regex
guard were reported before anyone read the query.

Note the sibling query disagrees: py/full-ssrf *does* accept an
`re.match`/`re.fullmatch` guard. The two are not interchangeable -- read the
query before assuming what counts as a sanitiser.
"""

from __future__ import annotations

import os


class PathEscape(ValueError):
    """A built path resolved outside the base directory it was joined under.

    A plain `ValueError` subclass rather than an `HTTPException`, because this
    module is used from the agent runtime (`core/`) as well as from routers,
    and a 400 raised outside a request is a lie about what went wrong. Routers
    translate it -- see the handler registered in `main.py`.
    """


def contained_path(base: str | os.PathLike[str], *parts: str) -> str:
    """Join *parts* under *base* and return the result only if it stays inside.

    Written check-then-return rather than raise-on-failure so the value callers
    receive can only come from the branch where containment held. A guard that
    only raises leaves the raw parameter in scope, and the raw parameter is
    what ends up reaching the filesystem -- which is both a real gap and what
    the analyser reports.

    Symlinks are resolved on both sides, so a link *inside* the base pointing
    to another location inside the base is fine, while one pointing out is not.
    The comparison is against ``base + os.sep`` and not ``base``: without the
    separator a sibling directory named ``uploads-evil`` shares the prefix and
    passes. The base itself is not a valid target either -- callers always want
    something under it.

    An absolute component makes ``os.path.join`` discard everything before it;
    that resolves outside the base and is rejected here rather than special
    cased.
    """
    base_real = os.path.realpath(str(base))
    candidate = os.path.realpath(os.path.join(base_real, *[str(p) for p in parts]))
    if candidate.startswith(base_real + os.sep):
        return candidate
    raise PathEscape(f"path escapes its base directory: {parts!r}")
