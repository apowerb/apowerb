"""The harness rules are proven, not assumed.

``scripts/harness.py`` only protects the tree if every rule still fires: a
pattern broken by an edit, or a rule removed, would leave the harness green and
empty. The counter-examples under ``tests/harness/fixtures/bad`` break each
rule once; the tree itself, and the ``good`` fixture, must pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _harness(*paths: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/harness.py", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _rule_ids() -> list[str]:
    config = json.loads((ROOT / "harness.config.json").read_text(encoding="utf-8"))
    return [rule["id"] for rule in config["rules"]]


def test_every_rule_fires_on_its_counter_example():
    result = _harness("tests/harness/fixtures/bad")

    # Exit 1 alone proves nothing (a crash exits 1 too): the harness must say
    # it blocked, and name each rule.
    assert result.returncode == 1, result.stderr
    assert "bloquante" in result.stderr
    silent = [rule for rule in _rule_ids() if f"[{rule}]" not in result.stderr]
    assert silent == [], f"no counter-example fires these rules: {silent}"


def test_the_safe_forms_pass():
    result = _harness("tests/harness/fixtures/good")

    assert result.returncode == 0, result.stderr
    assert "harness: OK" in result.stdout


def test_the_tree_passes_the_harness():
    result = _harness()

    assert result.returncode == 0, result.stderr
    assert "harness: OK" in result.stdout
