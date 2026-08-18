"""Naming the administrator by e-mail must be enough.

On a new install the person the operator designates has NOT signed up yet, so a
bootstrap that only promotes an existing account does nothing at all -- which is
exactly what happened on the first hosted deployment: `DEFAULT_SUPERADMIN_EMAIL`
was set, and the logs said "names an unknown account ... Nothing done".

Setting the e-mail alone is the whole configuration. Whoever signs up with that
address IS the administrator, with their own password, no restart required.
`DEFAULT_SUPERADMIN_PASSWORD` stays optional and only serves to create the
account up front, before anyone signs up.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apowerb.helpers.database import Base
from apowerb.helpers.default_superadmin import is_designated_superadmin
from apowerb.users import schemas, service


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    for t in Base.metadata.tables.values():
        t.schema = None
    Base.metadata.schema = None
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _signup(email):
    return schemas.UserCreate(
        email=email, password="Their0wn!Password", first_name="A", last_name="B"
    )


class TestDesignationByEmailAlone:
    async def test_the_designated_address_signs_up_as_admin(self, db, monkeypatch):
        monkeypatch.setenv("DEFAULT_SUPERADMIN_EMAIL", "boss@example.com")
        monkeypatch.delenv("DEFAULT_SUPERADMIN_PASSWORD", raising=False)

        created = await service.create_user(_signup("boss@example.com"), db)

        assert str(created.role).endswith("ADMIN")

    async def test_the_match_ignores_case_and_spacing(self, db, monkeypatch):
        monkeypatch.setenv("DEFAULT_SUPERADMIN_EMAIL", "  Boss@Example.COM  ")

        created = await service.create_user(_signup("boss@example.com"), db)

        assert str(created.role).endswith("ADMIN")

    async def test_anyone_else_signs_up_as_a_plain_user(self, db, monkeypatch):
        monkeypatch.setenv("DEFAULT_SUPERADMIN_EMAIL", "boss@example.com")

        created = await service.create_user(_signup("someone@example.com"), db)

        assert str(created.role).endswith("USER")

    async def test_without_the_variable_nobody_is_promoted(self, db, monkeypatch):
        monkeypatch.delenv("DEFAULT_SUPERADMIN_EMAIL", raising=False)

        created = await service.create_user(_signup("boss@example.com"), db)

        assert str(created.role).endswith("USER")


class TestTheDesignationPredicate:
    def test_empty_configuration_designates_nobody(self, monkeypatch):
        monkeypatch.delenv("DEFAULT_SUPERADMIN_EMAIL", raising=False)
        assert is_designated_superadmin("anyone@example.com") is False

        # A blank value is not a configuration -- otherwise a stray
        # `DEFAULT_SUPERADMIN_EMAIL=` would make the next signup an admin.
        monkeypatch.setenv("DEFAULT_SUPERADMIN_EMAIL", "   ")
        assert is_designated_superadmin("anyone@example.com") is False

    def test_a_missing_address_never_matches(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_SUPERADMIN_EMAIL", "boss@example.com")
        assert is_designated_superadmin("") is False
        assert is_designated_superadmin(None) is False
