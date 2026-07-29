"""Tests for the ctxvar-based DB binding of scei_ar_persist and scei_mail.

Written FIRST (TDD red phase). All DB access is mocked.
"""
from __future__ import annotations

import inspect
import os
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MSSQL_PARAMS = {
    "DB_TYPE": "mssql",
    "DB_HOST": "192.168.1.205",
    "DB_NAME": "SuiviAR",
    "DB_USER": "scei_user",
    "DB_PASSWORD": "s3cr3t",
    "DB_PORT": "1433",
    "DB_ENCRYPT": "no",
    "DB_TRUST_SERVER_CERTIFICATE": "yes",
    "DB_ODBC_DRIVER": "ODBC Driver 18 for SQL Server",
    "DB_ALLOWED_OPS": "SELECT,INSERT",
}

_POSTGRES_POISON_ENV = {
    "DB_HOST": "10.0.0.99",
    "DB_NAME": "defaultdb",
    "DB_USER": "pg_user",
    "DB_PASSWORD": "pg_pass",
    "DB_PORT": "5432",
    "DB_TYPE": "postgresql",
}


# ---------------------------------------------------------------------------
# (a) ctxvar overrides os.environ
# ---------------------------------------------------------------------------

class TestCtxvarOverridesEnv:
    def test_persist_uses_bound_host_not_env(self, monkeypatch):
        from th2customers.scei.tools import scei_ar_persist

        for k, v in _POSTGRES_POISON_ENV.items():
            monkeypatch.setenv(k, v)

        captured_urls = []

        def _fake_create_engine(url, **kwargs):
            captured_urls.append(url)
            return MagicMock()

        token = scei_ar_persist._persist_db_params.set(_MSSQL_PARAMS)
        try:
            with patch(
                "th2customers.scei.tools.scei_ar_persist.create_engine",
                side_effect=_fake_create_engine,
            ):
                scei_ar_persist._get_db_engine()
        finally:
            scei_ar_persist._persist_db_params.reset(token)

        assert captured_urls, "create_engine was not called"
        url = captured_urls[0]
        assert "192.168.1.205" in url, f"Bound host not in URL: {url!r}"
        assert "SuiviAR" in url, f"Bound DB not in URL: {url!r}"
        assert "10.0.0.99" not in url, f"Poisoned env host leaked into URL: {url!r}"
        assert "defaultdb" not in url, f"Poisoned env DB leaked into URL: {url!r}"

    def test_mail_uses_bound_host_not_env(self, monkeypatch):
        from th2customers.scei.tools import scei_mail

        for k, v in _POSTGRES_POISON_ENV.items():
            monkeypatch.setenv(k, v)

        captured_urls = []

        def _fake_create_engine(url, **kwargs):
            captured_urls.append(url)
            return MagicMock()

        token = scei_mail._mail_db_params.set(_MSSQL_PARAMS)
        try:
            with patch(
                "th2customers.scei.tools.scei_mail.create_engine",
                side_effect=_fake_create_engine,
            ):
                scei_mail._get_db_engine()
        finally:
            scei_mail._mail_db_params.reset(token)

        assert captured_urls, "create_engine was not called"
        url = captured_urls[0]
        assert "192.168.1.205" in url
        assert "SuiviAR" in url
        assert "10.0.0.99" not in url


# ---------------------------------------------------------------------------
# (b) make_persist_tool / make_scei_mail_tool preserve signature
# ---------------------------------------------------------------------------

class TestSignaturePreserved:
    def test_make_persist_tool_signature_matches_original(self):
        from th2customers.scei.tools.scei_ar_persist import (
            make_persist_tool,
            tool_persist_ar_record,
        )
        wrapped = make_persist_tool(_MSSQL_PARAMS)
        assert inspect.signature(wrapped) == inspect.signature(tool_persist_ar_record)

    def test_make_scei_mail_tool_signature_matches_original(self):
        from th2customers.scei.tools.scei_mail import (
            make_scei_mail_tool,
            tool_send_scei_mail,
        )
        wrapped = make_scei_mail_tool(_MSSQL_PARAMS)
        assert inspect.signature(wrapped) == inspect.signature(tool_send_scei_mail)


# ---------------------------------------------------------------------------
# (c) rebind_scei_persist — config selection logic
# ---------------------------------------------------------------------------

_CFG_PMI_NO_INSERT = {
    "tool_config_id": "tool_config10",
    "tool_name": "database",
    "tool_category": "database",
    "owner_id": "owner1",
    "tool_config_params": {
        "DB_TYPE": "mssql",
        "DB_HOST": "192.168.1.200",
        "DB_NAME": "PMI",
        "DB_USER": "pmi_user",
        "DB_PASSWORD": "pmi_pass",
        "DB_ALLOWED_OPS": "SELECT",
    },
}

_CFG_SUIVIAR_INSERT_ID15 = {
    "tool_config_id": "tool_config15",
    "tool_name": "database",
    "tool_category": "database",
    "owner_id": "owner1",
    "tool_config_params": {
        "DB_TYPE": "mssql",
        "DB_HOST": "192.168.1.205",
        "DB_NAME": "SuiviAR",
        "DB_USER": "scei_user",
        "DB_PASSWORD": "s3cr3t",
        "DB_ALLOWED_OPS": "SELECT,INSERT",
    },
}

_CFG_READONLY_SELECT_ID17 = {
    "tool_config_id": "tool_config17",
    "tool_name": "database",
    "tool_category": "database",
    "owner_id": "owner1",
    "tool_config_params": {
        "DB_TYPE": "mssql",
        "DB_HOST": "192.168.1.205",
        "DB_NAME": "SuiviAR_readonly",
        "DB_USER": "r_user",
        "DB_PASSWORD": "r_pass",
        "DB_ALLOWED_OPS": "SELECT",
    },
}


def _tool_func(name):
    def fn():
        pass
    fn.__name__ = name
    return fn


def _patch_list_configs(configs):
    return patch(
        "th2agent.tools_store.tools_helpers.list_user_tool_configs",
        return_value=configs,
    )


class TestRebindSceiPersist:
    def test_selects_mssql_insert_config_id15(self):
        from th2customers.scei.tools.scei_ar_persist import rebind_scei_persist
        from th2customers.scei.tools.scei_ar_persist import tool_persist_ar_record

        tools_funcs = [_tool_func("tool_other"), tool_persist_ar_record]
        configs = [_CFG_PMI_NO_INSERT, _CFG_SUIVIAR_INSERT_ID15, _CFG_READONLY_SELECT_ID17]

        with _patch_list_configs(configs):
            result = rebind_scei_persist("agent12", [], tools_funcs, owner_id="owner1")

        fn = next((f for f in result if getattr(f, "__name__", "") == "tool_persist_ar_record"), None)
        assert fn is not None
        assert fn is not tool_persist_ar_record, "Should be a wrapped closure, not the original"

    def test_two_write_configs_picks_lower_id(self):
        from th2customers.scei.tools.scei_ar_persist import rebind_scei_persist
        from th2customers.scei.tools.scei_ar_persist import tool_persist_ar_record
        import functools

        cfg_id20 = {
            **_CFG_SUIVIAR_INSERT_ID15,
            "tool_config_id": "tool_config20",
            "tool_config_params": {**_CFG_SUIVIAR_INSERT_ID15["tool_config_params"], "DB_NAME": "SuiviAR2"},
        }
        tools_funcs = [tool_persist_ar_record]
        configs = [cfg_id20, _CFG_SUIVIAR_INSERT_ID15]

        captured_params = []

        def _fake_make_persist_tool(params):
            captured_params.append(params)
            @functools.wraps(tool_persist_ar_record)
            def _w(*args, **kwargs):
                pass
            return _w

        with _patch_list_configs(configs):
            with patch(
                "th2customers.scei.tools.scei_ar_persist.make_persist_tool",
                side_effect=_fake_make_persist_tool,
            ):
                rebind_scei_persist("agent12", [], tools_funcs, owner_id="owner1")

        assert captured_params, "make_persist_tool was not called"
        assert captured_params[0]["DB_NAME"] == "SuiviAR",             f"Expected SuiviAR (id=15) but got {captured_params[0]["DB_NAME"]!r}"

    def test_no_write_config_fallback_no_rebind(self):
        from th2customers.scei.tools.scei_ar_persist import rebind_scei_persist
        from th2customers.scei.tools.scei_ar_persist import tool_persist_ar_record

        tools_funcs = [tool_persist_ar_record]
        configs = [_CFG_PMI_NO_INSERT, _CFG_READONLY_SELECT_ID17]

        with _patch_list_configs(configs):
            result = rebind_scei_persist("agent12", [], tools_funcs, owner_id="owner1")

        assert result[0] is tool_persist_ar_record

    def test_no_persist_tool_returns_unchanged(self):
        from th2customers.scei.tools.scei_ar_persist import rebind_scei_persist

        tools_funcs = [_tool_func("tool_other"), _tool_func("tool_run_sql")]
        original_names = [getattr(f, "__name__", "") for f in tools_funcs]

        with _patch_list_configs([_CFG_SUIVIAR_INSERT_ID15]):
            result = rebind_scei_persist("agent12", [], tools_funcs, owner_id="owner1")

        result_names = [getattr(f, "__name__", "") for f in result]
        assert result_names == original_names


# ---------------------------------------------------------------------------
# (d) ctxvar reset after closure call
# ---------------------------------------------------------------------------

class TestCtxvarResetAfterClosure:
    def test_persist_ctxvar_reset_after_call(self):
        from th2customers.scei.tools.scei_ar_persist import (
            make_persist_tool,
            _persist_db_params,
            tool_persist_ar_record,
        )

        assert _persist_db_params.get() is None

        call_log = []

        def _fake_tool(*args, **kwargs):
            call_log.append(_persist_db_params.get())
            return {"success": True, "commande_id": 1, "lignes_inserees": 0, "error": None}

        wrapped = make_persist_tool(_MSSQL_PARAMS)

        with patch(
            "th2customers.scei.tools.scei_ar_persist.tool_persist_ar_record",
            side_effect=_fake_tool,
        ):
            wrapped(
                NumeroCommande="CF0001",
                DateCommande="2026-05-22",
                StatutGlobal="conforme",
            )

        assert _persist_db_params.get() is None, "ctxvar leaked after closure call"
        assert call_log[0] == _MSSQL_PARAMS, "ctxvar not set during call"

    def test_persist_ctxvar_reset_even_on_exception(self):
        from th2customers.scei.tools.scei_ar_persist import (
            make_persist_tool,
            _persist_db_params,
        )

        assert _persist_db_params.get() is None

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        wrapped = make_persist_tool(_MSSQL_PARAMS)

        with patch(
            "th2customers.scei.tools.scei_ar_persist.tool_persist_ar_record",
            side_effect=_boom,
        ):
            with pytest.raises(RuntimeError):
                wrapped(
                    NumeroCommande="CF0001",
                    DateCommande="2026-05-22",
                    StatutGlobal="conforme",
                )

        assert _persist_db_params.get() is None, "ctxvar leaked after exception"


# ---------------------------------------------------------------------------
# (e) bound without DB_TYPE returns None + logs error
# ---------------------------------------------------------------------------

class TestBoundWithoutDbType:
    def test_persist_no_db_type_returns_none(self, caplog):
        import logging
        from th2customers.scei.tools import scei_ar_persist

        params_no_type = {k: v for k, v in _MSSQL_PARAMS.items() if k != "DB_TYPE"}

        token = scei_ar_persist._persist_db_params.set(params_no_type)
        try:
            with caplog.at_level(logging.ERROR, logger="th2customers.scei.tools.scei_ar_persist"):
                result = scei_ar_persist._get_db_engine()
        finally:
            scei_ar_persist._persist_db_params.reset(token)

        assert result is None
        assert any("DB_TYPE manquant" in r.message for r in caplog.records),             f"Expected DB_TYPE error log, got: {[r.message for r in caplog.records]}"

    def test_mail_no_db_type_returns_none(self, caplog):
        import logging
        from th2customers.scei.tools import scei_mail

        params_no_type = {k: v for k, v in _MSSQL_PARAMS.items() if k != "DB_TYPE"}

        token = scei_mail._mail_db_params.set(params_no_type)
        try:
            with caplog.at_level(logging.ERROR, logger="th2customers.scei.tools.scei_mail"):
                result = scei_mail._get_db_engine()
        finally:
            scei_mail._mail_db_params.reset(token)

        assert result is None
        assert any("DB_TYPE manquant" in r.message for r in caplog.records),             f"Expected DB_TYPE error log, got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# (f) system-owner configs are ignored (finding #2 - security)
# ---------------------------------------------------------------------------

_CFG_SYSTEM_MSSQL_INSERT = {
    "tool_config_id": "tool_config5",
    "tool_name": "database",
    "tool_category": "database",
    "owner_id": "system",
    "tool_config_params": {
        "DB_TYPE": "mssql",
        "DB_HOST": "10.0.0.1",
        "DB_NAME": "SystemDB",
        "DB_USER": "sys_user",
        "DB_PASSWORD": "sys_pass",
        "DB_ALLOWED_OPS": "SELECT,INSERT",
    },
}


class TestSystemOwnerExcluded:
    def test_persist_ignores_system_owner_config(self):
        """A config owned by system must never be selected for a real owner."""
        from th2customers.scei.tools.scei_ar_persist import rebind_scei_persist
        from th2customers.scei.tools.scei_ar_persist import tool_persist_ar_record
        import functools

        tools_funcs = [tool_persist_ar_record]
        configs = [_CFG_SYSTEM_MSSQL_INSERT]

        captured_params = []

        def _fake_make_persist_tool(params):
            captured_params.append(params)
            @functools.wraps(tool_persist_ar_record)
            def _w(*args, **kwargs):
                pass
            return _w

        with _patch_list_configs(configs):
            with patch(
                "th2customers.scei.tools.scei_ar_persist.make_persist_tool",
                side_effect=_fake_make_persist_tool,
            ):
                result = rebind_scei_persist("agent12", [], tools_funcs, owner_id="owner1")

        assert not captured_params, "make_persist_tool should NOT be called for system config"
        assert result[0] is tool_persist_ar_record

    def test_mail_ignores_system_owner_config(self):
        """A config owned by system must never be selected for scei_mail rebind."""
        from th2customers.scei.tools.scei_mail import rebind_scei_mail
        from th2customers.scei.tools.scei_mail import tool_send_scei_mail
        import functools

        tools_funcs = [tool_send_scei_mail]
        configs = [_CFG_SYSTEM_MSSQL_INSERT]

        captured_params = []

        def _fake_make_mail_tool(params):
            captured_params.append(params)
            @functools.wraps(tool_send_scei_mail)
            def _w(*args, **kwargs):
                pass
            return _w

        with _patch_list_configs(configs):
            with patch(
                "th2customers.scei.tools.scei_mail.make_scei_mail_tool",
                side_effect=_fake_make_mail_tool,
            ):
                result = rebind_scei_mail("agent12", [], tools_funcs, owner_id="owner1")

        assert not captured_params, "make_scei_mail_tool should NOT be called for system config"
        assert result[0] is tool_send_scei_mail

    def test_persist_skips_system_picks_real_owner_config(self):
        """When both a system config and a real owner config exist, the real one is used."""
        from th2customers.scei.tools.scei_ar_persist import rebind_scei_persist
        from th2customers.scei.tools.scei_ar_persist import tool_persist_ar_record
        import functools

        tools_funcs = [tool_persist_ar_record]
        configs = [_CFG_SYSTEM_MSSQL_INSERT, _CFG_SUIVIAR_INSERT_ID15]

        captured_params = []

        def _fake_make_persist_tool(params):
            captured_params.append(params)
            @functools.wraps(tool_persist_ar_record)
            def _w(*args, **kwargs):
                pass
            return _w

        with _patch_list_configs(configs):
            with patch(
                "th2customers.scei.tools.scei_ar_persist.make_persist_tool",
                side_effect=_fake_make_persist_tool,
            ):
                rebind_scei_persist("agent12", [], tools_funcs, owner_id="owner1")

        assert captured_params, "make_persist_tool should be called for real owner config"
        assert captured_params[0]["DB_NAME"] == "SuiviAR", "Expected SuiviAR (id=15) but got system DB instead"
