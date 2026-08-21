"""The Outlook Mail OAuth flow read four settings that `Settings` never declared.

`routers/emailing.py` asked for `settings.outlook_mail_client_id`,
`..._client_secret`, `..._tenant_id` and `..._redirect_uri`. None of the four
existed on the class -- `git log -S "outlook_mail_client_id: str"` returns
nothing over the whole history, so no released version ever had them.
`model_config` carries `extra="ignore"`, so setting the matching environment
variables created nothing either: the attribute was simply absent, and every
read raised `AttributeError`. Connecting an Outlook account failed outright,
whatever the operator put in the environment.

The credentials are not re-declared under the `outlook_mail_` name. The same
account's tokens are refreshed elsewhere with
`microsoft_integration_client_id` (`tools_store/portfolio/microsoft_auth.py`,
`integrations/outlook_webhook.py`); minting them from a second app
registration here would hand out tokens the refresh path cannot renew.

The callback is a public URL like any other, so it is deduced from
`app_public_url` in `configs/settings.py` rather than built in the router.
`test_derived_urls.py` covers the deduction itself, parametrised over the
table; what is left to prove here is that the router hands the value out
instead of raising.

`extra="ignore"` makes this whole class of fault silent until the line runs,
which is why the last tests read the tree instead of this router.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import apowerb
from apowerb.configs.settings import Settings

A_FRONT = "https://app.example.com"
CALLBACK_PATH = "/emailing/microsoft/callback"


def test_the_router_hands_out_the_callback_instead_of_raising():
    """The read that used to raise. Reached through the router's own module
    object, since that is where the absent attribute was consulted."""
    from apowerb.routers import emailing

    settings = Settings(app_public_url=A_FRONT)

    assert settings.outlook_mail_redirect_uri == f"{A_FRONT}{CALLBACK_PATH}"
    assert emailing.settings.outlook_mail_redirect_uri


def test_the_callback_is_guarded_like_the_public_url_it_is():
    """Shipped on localhost and named in the guard, so an install that
    configured its neighbours and forgot this one is told. An empty default
    would have been silent instead -- the guard only reports the settings
    whose shipped default mentions localhost."""
    from apowerb.configs.settings import _DERIVED_FROM_FRONT, _PUBLIC_URL_SETTINGS

    default = Settings.model_fields["outlook_mail_redirect_uri"].default

    assert "localhost" in default
    assert "outlook_mail_redirect_uri" in _PUBLIC_URL_SETTINGS
    assert _DERIVED_FROM_FRONT["outlook_mail_redirect_uri"] == CALLBACK_PATH


# ---------------------------------------------------------------------------
# The general fault, not just this one instance
# ---------------------------------------------------------------------------
# What the sweep below covers, and what it does not. An earlier version of it
# only understood `settings.<attr>`, which reads as a general guard while
# missing three shapes this codebase already uses in production:
# `getattr(settings, "x")` (helpers/api_schema.py, core/agent_helpers/default_llm.py),
# `self.settings.x` (storage/storage_service.py), and `get_settings` imported
# under another name (main.py, tools_store/portfolio/onedrive_read.py).
#
# It still cannot see three things: an attribute whose name is computed at
# runtime (`getattr(settings, name)`), a subscript, and a binding held under a
# name outside `_BARE_NAMES` (`cfg = get_settings()` then `cfg.x`). None occurs
# today. They are stated here rather than hidden, so a reader knows the size of
# the net instead of trusting the label.
_BARE_NAMES = {"settings", "_settings", "app_settings"}

# A local dict called `settings` is not this class. Ignoring the Mapping method
# names keeps `settings.get(...)` from failing the build, and costs nothing: no
# setting is called `get` or `items`.
_MAPPING_METHODS = {
    "get", "items", "keys", "values", "pop", "popitem",
    "setdefault", "update", "clear", "copy", "fromkeys",
}


def _declared() -> set[str]:
    return (
        set(Settings.model_fields)
        | set(Settings.model_computed_fields)
        | {name for name in dir(Settings) if not name.startswith("__")}
    )


def _settings_factories(tree: ast.AST) -> set[str]:
    """`get_settings`, plus every name it was imported under in this module."""
    names = {"get_settings"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            "configs.settings"
        ):
            names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "get_settings"
            )
    return names


def _is_settings(node: ast.AST, factories: set[str]) -> bool:
    """Does this expression evaluate to the Settings object?"""
    if isinstance(node, ast.Name):
        return node.id in _BARE_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr == "settings" and isinstance(node.value, ast.Name)
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id in factories
    return False


def _undeclared_reads(source: str, filename: str, declared: set[str]) -> list[str]:
    """Every read of a setting the class does not have. Reads the syntax tree
    rather than importing, so a module that never runs in the test suite is
    covered too."""
    tree = ast.parse(source, filename=filename)
    factories = _settings_factories(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_settings(node.value, factories):
            name, line = node.attr, node.lineno
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and _is_settings(node.args[0], factories)
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            name, line = node.args[1].value, node.lineno
        else:
            continue
        if name not in declared and name not in _MAPPING_METHODS:
            found.append(f"{filename}:{line}: {name}")
    return found


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param("value = settings.a_setting_nobody_declared\n", id="attribute"),
        pytest.param(
            'value = getattr(settings, "a_setting_nobody_declared", None)\n',
            id="getattr",
        ),
        pytest.param(
            "value = self.settings.a_setting_nobody_declared\n", id="self-attribute"
        ),
        pytest.param(
            "from apowerb.configs.settings import get_settings as gs\n"
            "value = gs().a_setting_nobody_declared\n",
            id="aliased-factory",
        ),
    ],
)
def test_the_scanner_reports_every_shape_of_undeclared_read(snippet):
    """Positive controls, one per shape the sweep claims to cover. Without
    them a scanner that silently matched nothing would let the sweep below
    pass on an empty result."""
    offences = _undeclared_reads(snippet, "synthetic.py", _declared())

    assert [o.split(": ")[-1] for o in offences] == ["a_setting_nobody_declared"]


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param("value = get_settings().frontend_urls\n", id="declared-field"),
        pytest.param(
            'settings = {"a": 1}\nvalue = settings.get("a")\n', id="local-dict"
        ),
    ],
)
def test_the_scanner_stays_quiet_on_what_is_not_a_fault(snippet):
    """Negative controls: a field the class really has, and a local dict that
    merely shares the name -- flagging the latter would fail the build on
    perfectly sound code."""
    assert _undeclared_reads(snippet, "synthetic.py", _declared()) == []


def test_no_module_reads_a_setting_that_settings_does_not_declare():
    """`extra="ignore"` turns a typo or a renamed field into an AttributeError
    raised only when that line happens to run -- for the Outlook callback,
    only once a user tried to connect an account."""
    root = pathlib.Path(apowerb.__file__).parent
    declared = _declared()

    modules = sorted(root.rglob("*.py"))
    offenders = [
        offence
        for path in modules
        for offence in _undeclared_reads(
            path.read_text(), str(path.relative_to(root.parent)), declared
        )
    ]

    assert modules, f"scanned nothing under {root}"
    assert offenders == []
