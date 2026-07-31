"""Unit tests for the overlay loader (no real overlay imported)."""

import sys
import types

import pytest

from apowerb.core.extensions import loader
from apowerb.core.extensions.registry import registry


def test_no_env_is_noop(monkeypatch):
    monkeypatch.delenv("TH2_OVERLAY_MODULE", raising=False)
    assert loader.load_overlay() is None


def test_loads_and_calls_init_overlay(monkeypatch):
    called = {}
    fake = types.ModuleType("fake_overlay_ok")
    fake.init_overlay = lambda reg: called.setdefault("reg", reg)
    sys.modules["fake_overlay_ok"] = fake
    monkeypatch.setenv("TH2_OVERLAY_MODULE", "fake_overlay_ok")
    try:
        assert loader.load_overlay() == "fake_overlay_ok"
        assert called["reg"] is registry
    finally:
        del sys.modules["fake_overlay_ok"]


def test_missing_init_overlay_fails_fast(monkeypatch):
    sys.modules["fake_overlay_bad"] = types.ModuleType("fake_overlay_bad")
    monkeypatch.setenv("TH2_OVERLAY_MODULE", "fake_overlay_bad")
    try:
        with pytest.raises(RuntimeError):
            loader.load_overlay()
    finally:
        del sys.modules["fake_overlay_bad"]
