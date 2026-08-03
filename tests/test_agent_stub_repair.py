"""Agent stubs must heal on their own, including the ones already written.

The package was renamed th2agent -> apowerb on 2026-07-31. The generator moved
with it; the stubs already on disk did not, because one is only rewritten when
its agent is saved again. On 2026-08-03 that left 124 of 128 agents on
production importing a module that no longer exists — each a
ModuleNotFoundError at its first run, and unnoticed because no agent had run
since the rename.

Repairing only what is *missing* was never enough: a stub that cannot import
the core is just as unusable as one that isn't there.
"""

from __future__ import annotations

import pytest

from apowerb.core import adk_agent_builder


class _Row:
    def __init__(self, agent_id):
        self._d = {"agent_id": agent_id}

    def _asdict(self):
        return self._d


class _FakeStore:
    """Stands in for the database: ensure_agent_modules only needs the ids."""

    class agent_table:
        @staticmethod
        def select():
            return "SELECT"

    def __init__(self, ids):
        self._ids = ids

    def get_list_agents(self, _query):
        return [_Row(i) for i in self._ids]


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setattr(adk_agent_builder, "agent_store", _FakeStore([164]))
    return tmp_path


def _stub(pool, body: str) -> "object":
    directory = pool / "agent164"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "agent.py"
    path.write_text(body)
    return path


class TestAStubThatCannotImportTheCoreIsRepaired:
    def test_the_renamed_import_is_rewritten(self, pool):
        path = _stub(pool, "\n# th2agent modules\nfrom th2agent.core.agent_helpers import to_agent\n")

        adk_agent_builder.ensure_agent_modules(str(pool))

        rewritten = path.read_text()
        assert "from apowerb.core.agent_helpers import to_agent" in rewritten
        assert "th2agent" not in rewritten
        # The agent it stands for must survive the repair.
        assert "to_agent(agent_name = 'agent164')" in rewritten

    def test_a_missing_stub_is_still_written(self, pool):
        adk_agent_builder.ensure_agent_modules(str(pool))

        path = pool / "agent164" / "agent.py"
        assert path.exists()
        assert "from apowerb.core.agent_helpers import to_agent" in path.read_text()
        assert (pool / "agent164" / "__init__.py").exists()

    def test_a_healthy_stub_is_left_untouched(self, pool):
        """Narrow on purpose: only what cannot import the core is rewritten, so
        a file someone actually customised keeps its contents."""
        custom = (
            "from apowerb.core.agent_helpers import to_agent\n"
            "# hand-written, and none of the repair's business\n"
            "root_agent = to_agent(agent_name = 'agent164')\n"
        )
        path = _stub(pool, custom)

        adk_agent_builder.ensure_agent_modules(str(pool))

        assert path.read_text() == custom

    def test_an_unreadable_stub_counts_as_broken(self, pool, monkeypatch):
        path = _stub(pool, "whatever")

        def boom(*args, **kwargs):
            raise OSError("unreadable")

        monkeypatch.setattr(adk_agent_builder, "open", boom, raising=False)
        assert adk_agent_builder._stub_cannot_import_the_core(str(path)) is True
