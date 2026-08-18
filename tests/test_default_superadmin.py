"""A fresh install must be able to get its first administrator.

Nothing in the open-source core promoted anyone: `role` defaults to USER, no
route exposes it (`UserUpdate` deliberately omits it, otherwise anyone could
promote themselves), and the only other path to ADMIN is the BYPASS_AUTH
developer bypass, which production refuses. The first administrator therefore
had to be written in by hand with SQL -- impossible on a hosted deployment where
the operator has no database console.

These two variables are the supported way in:

    DEFAULT_SUPERADMIN_EMAIL=someone@example.com
    DEFAULT_SUPERADMIN_PASSWORD=<used only to create a missing account>

Neither has a default: a shipped default password is a published one.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool

from apowerb.configs.settings import get_settings
from apowerb.helpers.core_tables import ensure_core_tables
from apowerb.helpers.default_superadmin import ensure_default_superadmin
from apowerb.models import User

SCHEMA = get_settings().db_schema or None


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    if SCHEMA:
        with eng.begin() as conn:
            conn.exec_driver_sql(f'ATTACH DATABASE \':memory:\' AS "{SCHEMA}"')
    ensure_core_tables(engine=eng)
    yield eng
    eng.dispose()


def _user(engine, email):
    with engine.begin() as conn:
        return conn.execute(
            select(User).where(User.email == email)
        ).mappings().first()


def _count(engine):
    with engine.begin() as conn:
        return len(conn.execute(select(User.user_id)).all())


class TestNothingConfigured:
    def test_does_nothing_without_an_email(self, engine, monkeypatch):
        monkeypatch.delenv("DEFAULT_SUPERADMIN_EMAIL", raising=False)
        monkeypatch.setenv("DEFAULT_SUPERADMIN_PASSWORD", "irrelevant")

        ensure_default_superadmin(engine=engine)

        assert _count(engine) == 0

    def test_an_empty_email_is_not_a_configuration(self, engine, monkeypatch):
        monkeypatch.setenv("DEFAULT_SUPERADMIN_EMAIL", "   ")
        monkeypatch.setenv("DEFAULT_SUPERADMIN_PASSWORD", "irrelevant")

        ensure_default_superadmin(engine=engine)

        assert _count(engine) == 0


class TestCreatingTheAccount:
    def test_creates_an_admin_on_an_empty_database(self, engine, monkeypatch):
        monkeypatch.setenv("DEFAULT_SUPERADMIN_EMAIL", "Boss@Example.com")
        monkeypatch.setenv("DEFAULT_SUPERADMIN_PASSWORD", "S3cret!Bootstrap")

        ensure_default_superadmin(engine=engine)

        row = _user(engine, "boss@example.com")
        assert row is not None, "the address is normalised to lower case"
        assert str(row["role"]).endswith("ADMIN")
        # Verified on purpose: the address comes from whoever deployed this,
        # and a verification mail they cannot receive would lock the only way in.
        assert row["email_verified"] is True
        # Never stored in clear, and it must match the core's own hasher --
        # a password hashed differently would simply never match at login.
        assert row["password"] != "S3cret!Bootstrap"
        from apowerb.helpers.security import verify_password

        assert verify_password("S3cret!Bootstrap", row["password"])

    def test_refuses_to_create_an_account_nobody_could_sign_into(
        self, engine, monkeypatch
    ):
        monkeypatch.setenv("DEFAULT_SUPERADMIN_EMAIL", "boss@example.com")
        monkeypatch.delenv("DEFAULT_SUPERADMIN_PASSWORD", raising=False)

        ensure_default_superadmin(engine=engine)

        assert _count(engine) == 0


class TestExistingAccount:
    @pytest.fixture
    def existing(self, engine):
        from apowerb.helpers.security import get_password_hash

        with engine.begin() as conn:
            conn.execute(
                User.__table__.insert().values(
                    email="boss@example.com",
                    first_name="Already",
                    last_name="Here",
                    password=get_password_hash("their-own-password"),
                    role="USER",
                )
            )
        return _user(engine, "boss@example.com")["password"]

    def test_promotes_without_touching_the_password(
        self, engine, existing, monkeypatch
    ):
        monkeypatch.setenv("DEFAULT_SUPERADMIN_EMAIL", "boss@example.com")
        monkeypatch.setenv("DEFAULT_SUPERADMIN_PASSWORD", "a-different-one")

        ensure_default_superadmin(engine=engine)

        row = _user(engine, "boss@example.com")
        assert str(row["role"]).endswith("ADMIN")
        # A bootstrap that reset the password on every restart would be a
        # backdoor: anyone holding the variable could take over a live account.
        assert row["password"] == existing
        assert _count(engine) == 1


class TestIdempotence:
    def test_running_twice_leaves_one_account(self, engine, monkeypatch):
        monkeypatch.setenv("DEFAULT_SUPERADMIN_EMAIL", "boss@example.com")
        monkeypatch.setenv("DEFAULT_SUPERADMIN_PASSWORD", "S3cret!Bootstrap")

        ensure_default_superadmin(engine=engine)
        ensure_default_superadmin(engine=engine)

        assert _count(engine) == 1
        assert str(_user(engine, "boss@example.com")["role"]).endswith("ADMIN")


class TestStartupIsNeverKilled:
    def test_a_database_failure_is_logged_not_raised(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_SUPERADMIN_EMAIL", "boss@example.com")
        monkeypatch.setenv("DEFAULT_SUPERADMIN_PASSWORD", "S3cret!Bootstrap")

        broken = create_engine("sqlite://")  # no tables at all

        # Losing the bootstrap administrator costs the control panel, not the
        # product. It must be loud in the logs and harmless at boot.
        ensure_default_superadmin(engine=broken)
