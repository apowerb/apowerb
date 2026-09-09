"""Proves that the usage recorder is actually wired into the
``after_model_callback`` chain of ``to_agent()`` -- not dead code.

The wiring changed nature: ``to_agent`` no longer names
``create_usage_recorder_callback``, it iterates the registry's observers.
The test must therefore follow the real link, not the old wording --
otherwise it would "pass" against a core that no longer calls anything at
all.

What is checked now:

- ``to_agent`` does consult ``model_observers`` (bytecode: robust to
  reformatting and to local-variable renames, unlike a substring search);
- it chains the result **before** writing ``after_model_cb`` into
  ``agent_kwargs`` -- the order can't be proven from ``co_names``, which is
  an unordered set, so this assertion re-reads the source instead;
- and above all: wired to a real registry, this extension's observer does
  produce a usable callback. That's the link the first two tests cannot see.

The full execution of ``to_agent()`` requires a live database
(``get_agent_details`` hits Postgres) and is out of scope for a unit test --
same constraint as ``tests/test_callback_wiring.py``.
"""
from __future__ import annotations

import inspect

from apowerb.core.agent_helpers.agent_utils import to_agent


def test_to_agent_consults_the_registrys_observers():
    assert "model_observers" in to_agent.__code__.co_names


def test_to_agent_always_chains_the_callbacks():
    assert "chain_after_model_callbacks" in to_agent.__code__.co_names


def test_chaining_precedes_writing_into_kwargs():
    src = inspect.getsource(to_agent)
    wiring_idx = src.index("chain_after_model_callbacks(_observateur, after_model_cb)")
    kwargs_idx = src.index('agent_kwargs["after_model_callback"] = after_model_cb')
    assert wiring_idx < kwargs_idx


def test_the_core_does_provide_a_callback_to_the_registry():
    """The link the bytecode doesn't show: the observer the core registers
    returns a usable callback, not ``None``."""
    from apowerb.core.extensions.registry import ExtensionRegistry
    from apowerb.core.usage_wiring import register_core_usage

    registry = ExtensionRegistry()
    register_core_usage(registry)

    factories = registry.model_observers()
    assert factories, "the core registered no observer"

    callback = factories[0](
        agent_id="agent1",
        agent_name="Test",
        owner_id="x@y.z",
        model_name="gemini/gemini-2.5-flash",
        billed_to_thaink2=True,
    )
    assert callable(callback)
