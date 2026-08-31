"""Pre-flight validator for ADK agent prompts.

ADK interpolates **any** brace block matching ``{+[^{}]*}+`` in
``agent_instruction``. For each match, it strips *all* surrounding braces
(``lstrip('{').rstrip('}')``), checks whether the result is a valid state
name (``_is_valid_state_name`` → ``isidentifier()`` or
``<prefix>:<identifier>`` with prefix in ``{app:, user:, temp:}``), and if
so looks it up in ``session.state``. Missing → ``KeyError``. The optional
``?`` suffix makes the lookup return ``''`` instead of raising.

Two consequences that surprised people on the 2026-05-13 production
incident:

* **``{{xxx}}`` is NOT an escape.** ADK strips *all* braces, so
  ``{{keyword}}`` → ``keyword`` → KeyError just like ``{keyword}``.
  The only safe way to render a literal brace block is to make the
  content fail ``isidentifier()`` (use ``<keyword>``, a comma list,
  quotes, dots, etc.) or to bind the name in state.
* **``{4,6}``, ``{a, b, c}``, ``{"k": v}`` are already safe** at runtime
  because their stripped content is not an identifier; ADK leaves them
  literal. Doubling those braces is a no-op (PR #172 doubled them
  anyway; they were never the runtime bug).

The actual runtime bug on 2026-05-13 was ``{keyword}`` (then re-broken
as ``{{keyword}}`` in PR #172). This module reproduces ADK's exact
logic so a smoke test against the shipped templates cracks on the same
patterns ADK would crack on at runtime.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

_logger = logging.getLogger(__name__)


# Same pattern ADK uses in google.adk.utils.instructions_utils:
#   ``{+[^{}]*}+`` — one-or-more opening braces, no nested braces in
#   the body, one-or-more closing braces.
_ADK_PLACEHOLDER_RE = re.compile(r"\{+[^{}]*\}+")

_ADK_STATE_PREFIXES = {"app:", "user:", "temp:"}


@dataclass(frozen=True)
class Issue:
    level: str  # "error" (lookup will crash) | "warning" (optional ? — silent empty)
    placeholder: str  # raw match including all surrounding braces
    var_name: str  # what ADK will actually look up in session.state
    line: int  # 1-indexed
    reason: str


def _adk_var_name(raw: str) -> tuple[str | None, bool]:
    """Replicate ADK's resolution logic. Returns ``(var_name, is_optional)``
    where ``var_name`` is the actual state key ADK will look up, or
    ``None`` if ADK would leave the match literal (i.e. not a valid state
    name)."""
    name = raw.lstrip("{").rstrip("}").strip()
    optional = name.endswith("?")
    if optional:
        name = name.removesuffix("?")

    parts = name.split(":")
    if len(parts) == 1:
        return (name, optional) if name.isidentifier() else (None, optional)
    if len(parts) == 2:
        if (parts[0] + ":") in _ADK_STATE_PREFIXES and parts[1].isidentifier():
            return name, optional
    return None, optional


def find_unsafe_braces(
    text: str, known_keys: Iterable[str] | None = None
) -> list[Issue]:
    """Scan ``text`` for placeholders ADK would resolve at runtime against
    ``session.state``. Returns the ones that have no upstream binder."""
    if not text:
        return []

    known = set(known_keys) if known_keys else set()
    issues: list[Issue] = []

    for match in _ADK_PLACEHOLDER_RE.finditer(text):
        raw = match.group(0)
        var_name, optional = _adk_var_name(raw)
        if var_name is None:
            continue  # ADK leaves literal — safe
        if var_name in known:
            continue  # upstream sub-agent binds it — safe

        line = text.count("\n", 0, match.start()) + 1
        if optional:
            issues.append(
                Issue(
                    level="warning",
                    placeholder=raw,
                    var_name=var_name,
                    line=line,
                    reason=(
                        f"`{raw}` resolves to `session.state[{var_name!r}]` at "
                        f"runtime. The `?` suffix means ADK returns empty "
                        f"string if missing (no crash), but the operator "
                        f"probably meant the placeholder to render. Bind it "
                        f"via an upstream `output_key={var_name!r}` or rewrite "
                        f"as `<{var_name}>` to render literally."
                    ),
                )
            )
        else:
            issues.append(
                Issue(
                    level="error",
                    placeholder=raw,
                    var_name=var_name,
                    line=line,
                    reason=(
                        f"`{raw}` resolves to `session.state[{var_name!r}]` at "
                        f"runtime. Missing key → ADK raises KeyError and "
                        f"/run returns 500. Note: `{{{{{var_name}}}}}` is "
                        f"NOT an escape — ADK strips all braces. To render "
                        f"`{{{var_name}}}` literally, use `<{var_name}>` or "
                        f"make the brace content non-identifier (commas, "
                        f"quotes, dots). Or bind via upstream "
                        f"`output_key={var_name!r}`."
                    ),
                )
            )

    return issues


def collect_known_keys(templates) -> set[str]:
    """Collect every ``output_key`` declared across templates. These are
    legitimate ``session.state`` keys that downstream sub-agents in a
    SequentialAgent can reference via ``{output_key}`` in their prompts."""
    keys: set[str] = set()
    for tpl in templates:
        k = tpl.get("output_key")
        if k:
            keys.add(k)
    return keys


def validate_templates(
    templates,
    extra_known_keys: Iterable[str] | None = None,
) -> list[tuple[str, list[Issue]]]:
    """Apply ``find_unsafe_braces`` to each template's ``agent_instruction``.

    Cross-template ``output_key`` values are collected first and treated
    as legitimate state keys — so a sub-agent prompt referencing
    ``{ar_intake}`` (bound by the upstream intake sub-agent's
    ``output_key='ar_intake'``) does not trigger a false positive.
    ``extra_known_keys`` lets the caller inject globally available state
    vars (e.g. webhook context fields)."""
    templates = list(templates)  # we iterate twice
    known = collect_known_keys(templates)
    if extra_known_keys:
        known.update(extra_known_keys)

    results: list[tuple[str, list[Issue]]] = []
    for tpl in templates:
        instruction = tpl.get("agent_instruction")
        if not instruction:
            continue
        issues = find_unsafe_braces(instruction, known_keys=known)
        if issues:
            results.append((tpl.get("name", "<unnamed>"), issues))
    return results


def assert_templates_safe(templates: Iterable[dict]) -> None:
    """Boot-time gate. Raises ``ValueError`` on **error-level** issues.
    Warnings are logged but do not raise."""
    results = validate_templates(templates)
    error_lines: list[str] = []
    warn_lines: list[str] = []

    for name, issues in results:
        for issue in issues:
            line = (
                f"  [{issue.level.upper()}] template={name!r} "
                f"line={issue.line} placeholder={issue.placeholder!r} "
                f"var={issue.var_name!r} — {issue.reason}"
            )
            if issue.level == "error":
                error_lines.append(line)
            else:
                warn_lines.append(line)

    for w in warn_lines:
        _logger.warning(w)

    if error_lines:
        msg = (
            "Pre-flight check failed: agent template(s) contain brace "
            "placeholders that ADK will resolve against `session.state` "
            "at runtime, with no upstream binder — they will raise "
            "KeyError and break /run. To render a literal brace block, "
            "use `<name>` or a non-identifier content; doubling braces "
            "is NOT an escape.\n" + "\n".join(error_lines)
        )
        raise ValueError(msg)
