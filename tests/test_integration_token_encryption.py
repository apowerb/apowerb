"""Unit tests — OAuth token encryption at rest for the Integration table.

Phase RED of TDD for batch B7:
- These tests describe the BEHAVIOUR we want from the unified integration
  token helpers (save / get / migrate).  The corresponding production code
  does NOT exist yet — importing the symbols below MUST fail today.
- When the green phase lands, ``apowerb.integrations.helpers`` must expose
  ``save_integration_tokens``, ``get_integration_tokens`` and
  ``encrypt_legacy_integration_tokens`` and every test below must pass.

Scope covered:

1. Round-trip ............ save then read returns the exact plaintext.
2. Storage ............... raw ``SELECT access_token`` never contains the
                           plaintext (proof it is encrypted at rest).
3. Legacy migration ...... a row inserted in clear is re-encrypted by the
                           migration helper; ``get_integration_tokens``
                           still returns the plaintext.
4. Missing ENCRYPT_KEY ... saving raises a clear error — never a silent
                           plaintext fallback.
5. Invalid ENCRYPT_KEY ... reading raises a readable error.
6. All providers ......... encryption is uniform across google / microsoft /
                           github / linkedin / odoo.
7. Nullable tokens ....... passing ``access_token=None`` (e.g. Odoo which
                           has no refresh_token) stays ``None`` without
                           raising an empty-ciphertext error.

The tests use an in-memory SQLite store that mimics the columns of the
real ``integrations`` table.  They do not hit the network or a live DB.
"""

from __future__ import annotations

import importlib
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    insert,
    select,
)
from sqlalchemy.engine import Engine


ALL_PROVIDERS = ["google", "microsoft", "github", "linkedin", "odoo"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fernet_key() -> str:
    """Fresh Fernet key used for the duration of a test."""
    return Fernet.generate_key().decode()


@pytest.fixture()
def active_fernet(fernet_key):
    """Force apowerb.helpers.encryptor to use a known-good Fernet instance.

    Restores the original one on teardown so later tests are not impacted.
    """
    from apowerb.helpers import encryptor as enc_mod

    original = enc_mod.fernet
    enc_mod.fernet = Fernet(fernet_key.encode())
    try:
        yield enc_mod.fernet
    finally:
        enc_mod.fernet = original


@pytest.fixture()
def sqlite_engine() -> Engine:
    """In-memory SQLite engine with an ``integrations`` table mirroring the
    real schema enough to round-trip the fields we care about."""
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "integrations",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("user_id", Integer, nullable=False),
        Column("provider", String(50), nullable=False),
        Column("provider_user_id", String, nullable=True),
        Column("provider_username", String, nullable=True),
        Column("access_token", String, nullable=True),
        Column("refresh_token", String, nullable=True),
        Column("scopes", String, nullable=True),
        Column("meta", JSON, nullable=True),
        Column("created_at", DateTime, nullable=True),
        Column("updated_at", DateTime, nullable=True),
        UniqueConstraint("user_id", "provider", name="uq_integration_user_provider"),
    )
    metadata.create_all(engine)
    return engine


@pytest.fixture()
def helpers_module(active_fernet, sqlite_engine, monkeypatch):
    """Import (or fail to import) the unified integration helpers module.

    In phase RED this import MUST fail, because the helpers do not exist
    yet.  We still return the module so individual tests can surface a
    precise AttributeError on the missing symbol.
    """
    mod = importlib.import_module("apowerb.integrations.helpers")

    # The production helpers rely on ``DBConfig().get_db_url()`` to build
    # their own engine.  For the unit tests we monkeypatch the engine
    # builder so every helper call lands on our in-memory SQLite.
    def _fake_engine(*_a, **_k):
        return sqlite_engine

    monkeypatch.setattr(mod, "_build_engine", _fake_engine, raising=False)
    return mod


# ---------------------------------------------------------------------------
# Small DB helpers — keep the tests readable
# ---------------------------------------------------------------------------


def _raw_select_tokens(engine: Engine, user_id: int, provider: str) -> tuple[Any, Any]:
    """Return the *raw* (access_token, refresh_token) bytes stored in DB."""
    table = Table("integrations", MetaData(), autoload_with=engine)
    with engine.connect() as conn:
        row = conn.execute(
            select(table.c.access_token, table.c.refresh_token).where(
                (table.c.user_id == user_id) & (table.c.provider == provider)
            )
        ).first()
    assert row is not None, f"no row for user_id={user_id} provider={provider}"
    return row[0], row[1]


def _raw_insert_plain(
    engine: Engine,
    user_id: int,
    provider: str,
    access_token: str,
    refresh_token: str,
) -> None:
    """Insert a row with PLAINTEXT tokens — simulates pre-migration data."""
    table = Table("integrations", MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(
            insert(table).values(
                user_id=user_id,
                provider=provider,
                provider_user_id="42",
                provider_username="legacy@example.com",
                access_token=access_token,
                refresh_token=refresh_token,
                scopes="",
                meta={},
            )
        )


# ---------------------------------------------------------------------------
# 1. Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_save_then_get_returns_plaintext(self, helpers_module, provider):
        helpers_module.save_integration_tokens(
            user_id=1,
            provider=provider,
            access_token="plain-access",
            refresh_token="plain-refresh",
            provider_username="alice@example.com",
            provider_user_id="u-42",
            scopes="read write",
            meta={"display_name": "Alice"},
        )

        tokens = helpers_module.get_integration_tokens(user_id=1, provider=provider)

        assert tokens is not None
        assert tokens["access_token"] == "plain-access"
        assert tokens["refresh_token"] == "plain-refresh"


# ---------------------------------------------------------------------------
# 2. Storage is ciphertext
# ---------------------------------------------------------------------------


class TestCiphertextAtRest:
    @pytest.mark.parametrize("provider", ALL_PROVIDERS)
    def test_raw_db_does_not_contain_plaintext(
        self, helpers_module, sqlite_engine, provider
    ):
        plain_a = "plain-access-do-not-leak"
        plain_r = "plain-refresh-do-not-leak"

        helpers_module.save_integration_tokens(
            user_id=7,
            provider=provider,
            access_token=plain_a,
            refresh_token=plain_r,
        )

        raw_a, raw_r = _raw_select_tokens(sqlite_engine, user_id=7, provider=provider)

        assert raw_a is not None
        assert raw_r is not None
        assert plain_a not in str(raw_a), (
            f"{provider}: plaintext access_token leaked into DB — got {raw_a!r}"
        )
        assert plain_r not in str(raw_r), (
            f"{provider}: plaintext refresh_token leaked into DB — got {raw_r!r}"
        )


# ---------------------------------------------------------------------------
# 3. Legacy migration
# ---------------------------------------------------------------------------


class TestLegacyMigration:
    def test_migration_encrypts_plaintext_row_in_place(
        self, helpers_module, sqlite_engine
    ):
        _raw_insert_plain(
            sqlite_engine,
            user_id=3,
            provider="github",
            access_token="legacy-access",
            refresh_token="legacy-refresh",
        )

        # Sanity check — row really is plaintext before the migration.
        raw_before = _raw_select_tokens(sqlite_engine, user_id=3, provider="github")
        assert raw_before == ("legacy-access", "legacy-refresh")

        migrated = helpers_module.encrypt_legacy_integration_tokens()

        # Helper MUST report at least the single row it migrated.
        assert migrated >= 1

        # Storage is no longer plaintext…
        raw_after = _raw_select_tokens(sqlite_engine, user_id=3, provider="github")
        assert raw_after[0] != "legacy-access"
        assert raw_after[1] != "legacy-refresh"

        # …but reading through the API still returns the original values.
        tokens = helpers_module.get_integration_tokens(user_id=3, provider="github")
        assert tokens == {
            "access_token": "legacy-access",
            "refresh_token": "legacy-refresh",
        } | {k: v for k, v in tokens.items() if k not in {"access_token", "refresh_token"}}

    def test_migration_is_idempotent(self, helpers_module, sqlite_engine):
        """Running the migration twice must not re-encrypt already-encrypted data."""
        _raw_insert_plain(
            sqlite_engine,
            user_id=4,
            provider="google",
            access_token="legacy-plain",
            refresh_token="legacy-refresh",
        )

        helpers_module.encrypt_legacy_integration_tokens()
        raw_first_pass = _raw_select_tokens(sqlite_engine, user_id=4, provider="google")

        helpers_module.encrypt_legacy_integration_tokens()
        raw_second_pass = _raw_select_tokens(sqlite_engine, user_id=4, provider="google")

        assert raw_first_pass == raw_second_pass
        # And the plaintext is still readable through the API.
        tokens = helpers_module.get_integration_tokens(user_id=4, provider="google")
        assert tokens["access_token"] == "legacy-plain"
        assert tokens["refresh_token"] == "legacy-refresh"


# ---------------------------------------------------------------------------
# 4. Missing ENCRYPT_KEY
# ---------------------------------------------------------------------------


class TestMissingEncryptKey:
    def test_save_raises_without_encrypt_key(self, helpers_module):
        """With no Fernet instance configured, save must FAIL LOUDLY —
        never silently fall back to plaintext persistence."""
        from apowerb.helpers import encryptor as enc_mod

        with patch.object(enc_mod, "fernet", None):
            with pytest.raises(Exception) as excinfo:
                helpers_module.save_integration_tokens(
                    user_id=5,
                    provider="github",
                    access_token="should-never-land-in-db",
                    refresh_token="",
                )
            msg = str(excinfo.value).lower()
            assert ("encrypt" in msg) or ("key" in msg) or ("fernet" in msg), (
                f"error message should mention missing encryption key, got: {excinfo.value!r}"
            )


# ---------------------------------------------------------------------------
# 5. Invalid ENCRYPT_KEY
# ---------------------------------------------------------------------------


class TestInvalidEncryptKey:
    def test_get_raises_readable_error_when_key_is_wrong(
        self, helpers_module, sqlite_engine
    ):
        # Save with the active Fernet from the fixture…
        helpers_module.save_integration_tokens(
            user_id=6,
            provider="microsoft",
            access_token="plain-access",
            refresh_token="plain-refresh",
        )

        # …then swap the Fernet to a fresh (incompatible) key and try to read.
        from apowerb.helpers import encryptor as enc_mod

        wrong_key = Fernet.generate_key()
        with patch.object(enc_mod, "fernet", Fernet(wrong_key)):
            with pytest.raises(Exception) as excinfo:
                helpers_module.get_integration_tokens(
                    user_id=6, provider="microsoft"
                )
            msg = str(excinfo.value).lower()
            assert ("decrypt" in msg) or ("token" in msg) or ("invalid" in msg), (
                f"error message should explain the decryption failure, got: {excinfo.value!r}"
            )


# ---------------------------------------------------------------------------
# 7. Nullable tokens
# ---------------------------------------------------------------------------


class TestNullableTokens:
    def test_none_access_token_stays_none(self, helpers_module, sqlite_engine):
        """Odoo rows and refresh-less flows pass ``refresh_token=None``.
        Encrypting None must not crash; reading must yield None — not an
        empty decrypted string."""
        helpers_module.save_integration_tokens(
            user_id=9,
            provider="odoo",
            access_token=None,
            refresh_token=None,
        )

        raw_a, raw_r = _raw_select_tokens(sqlite_engine, user_id=9, provider="odoo")
        assert raw_a is None
        assert raw_r is None

        tokens = helpers_module.get_integration_tokens(user_id=9, provider="odoo")
        assert tokens is not None
        assert tokens["access_token"] is None
        assert tokens["refresh_token"] is None


# ---------------------------------------------------------------------------
# Absence → None (sanity)
# ---------------------------------------------------------------------------


class TestAbsentIntegration:
    def test_get_returns_none_for_unknown_user(self, helpers_module):
        assert (
            helpers_module.get_integration_tokens(user_id=999, provider="github")
            is None
        )


# ---------------------------------------------------------------------------
# 8. Plaintext guards — DB CheckConstraint + ORM listener
# ---------------------------------------------------------------------------
#
# These tests exercise the defense-in-depth introduced after the 2026-05-07
# SCEI incident, where a manual SQL INSERT bypassed save_integration_tokens()
# and stored an OAuth token in plaintext.
#
# Two layers are validated:
#   1. SQLAlchemy ORM ``before_insert`` / ``before_update`` listener — fires
#      whenever an ``Integration(...)`` instance goes through a Session.
#   2. PostgreSQL/SQLite ``CheckConstraint`` on the ``integrations`` table —
#      fires for ANY write, including raw psql or Core ``Table.insert()``.


@pytest.fixture()
def orm_engine(active_fernet) -> Engine:
    """In-memory SQLite engine where the ``integrations`` table is created
    via the real ORM model, so its CheckConstraints are exercised.

    The shared ``Base`` metadata is bound to ``settings.db_schema`` (e.g.
    ``th2agent_dev``). SQLite has no notion of schemas, so we ATTACH an
    in-memory schema of the same name to make ``CREATE TABLE
    <schema>.integrations`` work out of the box.
    """
    from sqlalchemy import text
    from apowerb.configs.settings import get_settings
    from apowerb.models import Integration

    schema = get_settings().db_schema
    engine = create_engine("sqlite:///:memory:")
    if schema and schema != "public":
        with engine.begin() as conn:
            conn.execute(text(f"ATTACH DATABASE ':memory:' AS {schema}"))
    Integration.__table__.create(engine, checkfirst=True)
    return engine


@pytest.fixture()
def orm_session(orm_engine):
    from sqlalchemy.orm import Session

    with Session(orm_engine) as s:
        yield s


def _fernet_ciphertext(plaintext: str) -> str:
    """Produce a real Fernet ciphertext using the active fixture key."""
    from apowerb.helpers import encryptor as enc_mod

    return enc_mod.encrypt_value(plaintext)


class TestORMListenerBlocksPlaintext:
    def test_insert_plaintext_via_orm_raises(self, orm_session):
        """Adding an Integration with a plaintext access_token through the
        ORM Session must trigger the before_insert listener and raise."""
        from sqlalchemy.exc import StatementError
        from apowerb.models import Integration
        # Importing helpers registers the event listener as a side effect.
        import apowerb.integrations.helpers  # noqa: F401

        orm_session.add(
            Integration(
                user_id=100,
                provider="microsoft",
                access_token="eyJ.this.looks.like.a.raw.JWT",
                refresh_token=None,
            )
        )
        with pytest.raises((RuntimeError, StatementError)) as excinfo:
            orm_session.flush()
        assert "fernet" in str(excinfo.value).lower() or "encrypt" in str(excinfo.value).lower()

    def test_insert_valid_ciphertext_via_orm_passes(self, orm_session):
        """A real Fernet ciphertext must go through the listener untouched."""
        from apowerb.models import Integration
        import apowerb.integrations.helpers  # noqa: F401

        ciphertext = _fernet_ciphertext("plain-access")

        orm_session.add(
            Integration(
                user_id=101,
                provider="github",
                access_token=ciphertext,
                refresh_token=None,
            )
        )
        orm_session.flush()  # must not raise
        orm_session.commit()

    def test_null_tokens_pass_listener(self, orm_session):
        """``Integration(access_token=None, refresh_token=None)`` is a
        legitimate state (e.g. Odoo) — listener must let it through."""
        from apowerb.models import Integration
        import apowerb.integrations.helpers  # noqa: F401

        orm_session.add(
            Integration(
                user_id=102,
                provider="odoo",
                access_token=None,
                refresh_token=None,
            )
        )
        orm_session.flush()
        orm_session.commit()

    def test_update_plaintext_via_orm_raises(self, orm_session):
        """Mutating an Integration's access_token to plaintext via ORM must
        also fire the before_update listener."""
        from sqlalchemy.exc import StatementError
        from apowerb.models import Integration
        import apowerb.integrations.helpers  # noqa: F401

        ciphertext = _fernet_ciphertext("plain-access")
        row = Integration(
            user_id=103,
            provider="google",
            access_token=ciphertext,
            refresh_token=None,
        )
        orm_session.add(row)
        orm_session.commit()

        row.access_token = "eyJ.fresh.plaintext"
        with pytest.raises((RuntimeError, StatementError)) as excinfo:
            orm_session.flush()
        assert "fernet" in str(excinfo.value).lower() or "encrypt" in str(excinfo.value).lower()


class TestCheckConstraintBlocksRawInsert:
    """The DB-level CheckConstraint is the strong barrier — it fires for
    raw Core ``Table.insert()`` too, which the ORM listener does NOT see."""

    def test_raw_core_insert_plaintext_raises(self, orm_engine):
        """An INSERT through SQLAlchemy Core (or psql) bypassing the ORM
        must still be rejected by the table's CheckConstraint."""
        from sqlalchemy.exc import IntegrityError
        from apowerb.models import Integration

        with orm_engine.begin() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(
                    Integration.__table__.insert().values(
                        user_id=200,
                        provider="microsoft",
                        access_token="eyJ.plain.jwt",
                        refresh_token=None,
                    )
                )

    def test_raw_core_insert_valid_ciphertext_passes(self, orm_engine):
        """A row with a Fernet-prefixed access_token must satisfy the
        constraint even when inserted via raw Core."""
        from apowerb.models import Integration

        ciphertext = _fernet_ciphertext("plain-access")
        with orm_engine.begin() as conn:
            conn.execute(
                Integration.__table__.insert().values(
                    user_id=201,
                    provider="microsoft",
                    access_token=ciphertext,
                    refresh_token=None,
                )
            )

    def test_raw_core_insert_null_tokens_passes(self, orm_engine):
        from apowerb.models import Integration

        with orm_engine.begin() as conn:
            conn.execute(
                Integration.__table__.insert().values(
                    user_id=202,
                    provider="odoo",
                    access_token=None,
                    refresh_token=None,
                )
            )

    def test_raw_core_update_to_plaintext_raises(self, orm_engine):
        """UPDATE setting access_token back to plaintext must also be
        rejected by the CheckConstraint."""
        from sqlalchemy.exc import IntegrityError
        from apowerb.models import Integration

        ciphertext = _fernet_ciphertext("plain-access")
        with orm_engine.begin() as conn:
            conn.execute(
                Integration.__table__.insert().values(
                    user_id=203,
                    provider="github",
                    access_token=ciphertext,
                    refresh_token=None,
                )
            )

        with orm_engine.begin() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(
                    Integration.__table__.update()
                    .where(Integration.__table__.c.user_id == 203)
                    .values(access_token="eyJ.regression.plain")
                )
