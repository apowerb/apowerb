"""Tables for groups, their members and their permissions.

Registered as a bootstrap hook, never run at import time, following the
same rule as the usage extension: startup owns schema changes so a plain
import can never touch a shared database.

`run_service.is_admin` already reads `user.role`, which the core owns.
Nothing here duplicates it — a group grants permissions, it does not grant
the ADMIN role.
"""

from __future__ import annotations

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.database_connection import DBConfig
from sqlalchemy import create_engine, inspect, text

logger = setup_logging(__name__)
settings = get_settings()


def ensure_admin_tables() -> None:
    """Create the group and organisation tables when absent.

    Safe to call repeatedly; each table is checked on its own so a
    database created before organisations existed catches up here.
    """
    schema = settings.db_schema
    try:
        sync_url = DBConfig().get_db_url().replace("postgresql+asyncpg://", "postgresql://")
        engine = create_engine(sync_url, echo=False)

        with engine.connect() as conn:
            present = set(inspect(engine).get_table_names(schema=schema))

            if "admin_group" not in present:
                logger.info("Creating 'admin_group' table...")
                conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {schema}.admin_group (
                        group_id    SERIAL PRIMARY KEY,
                        name        VARCHAR(120) NOT NULL UNIQUE,
                        description VARCHAR(500),
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))

            # ON DELETE CASCADE on the group side only: deleting a group must
            # take its memberships with it, while removing a user is a core
            # concern this extension does not own.
            if "admin_group_member" not in present:
                logger.info("Creating 'admin_group_member' table...")
                conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {schema}.admin_group_member (
                        group_id   INTEGER NOT NULL
                                   REFERENCES {schema}.admin_group(group_id) ON DELETE CASCADE,
                        user_id    INTEGER NOT NULL,
                        added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (group_id, user_id)
                    )
                """))

            if "admin_group_permission" not in present:
                logger.info("Creating 'admin_group_permission' table...")
                conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {schema}.admin_group_permission (
                        group_id   INTEGER NOT NULL
                                   REFERENCES {schema}.admin_group(group_id) ON DELETE CASCADE,
                        permission VARCHAR(120) NOT NULL,
                        PRIMARY KEY (group_id, permission)
                    )
                """))


            if "admin_organization" not in present:
                logger.info("Creating 'admin_organization' table...")
                conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {schema}.admin_organization (
                        org_id      SERIAL PRIMARY KEY,
                        name        VARCHAR(120) NOT NULL UNIQUE,
                        description VARCHAR(500),
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))

            # A user belongs to at most one organisation: the primary key is
            # the user, not the pair. Two organisations for one person would
            # make "the users I administer" ambiguous, which is the whole
            # point of the scope.
            if "admin_org_member" not in present:
                logger.info("Creating 'admin_org_member' table...")
                conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {schema}.admin_org_member (
                        user_id  INTEGER PRIMARY KEY,
                        org_id   INTEGER NOT NULL
                                 REFERENCES {schema}.admin_organization(org_id) ON DELETE CASCADE,
                        added_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))

            # Who may cross organisation boundaries. Deliberately a table and
            # not a role value: `role_enum` is shared by six schemas on this
            # instance, including production, and adding to it is one-way.
            if "admin_superadmin" not in present:
                logger.info("Creating 'admin_superadmin' table...")
                conn.execute(text(f"""
                    CREATE TABLE IF NOT EXISTS {schema}.admin_superadmin (
                        user_id    INTEGER PRIMARY KEY,
                        granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                """))
            conn.commit()
        logger.info("[ADMIN] group and organisation tables ready")
    except Exception as exc:  # noqa: BLE001 -- a failed migration must be loud, not fatal at boot
        logger.error("[ADMIN] could not ensure group tables: %s", exc)


def ensure_superadmin_grant() -> None:
    """Promote (or create) the account named by the environment.

    Runs at every startup and is idempotent: it only ever *adds* the
    superadmin row, and only creates the account when it is absent.

    On a fresh install nobody is in `admin_superadmin`, so there is no
    control panel and no way to manage users — the first administrator had
    to be written in by hand. These two variables are the supported way in:

        DEFAULT_SUPERADMIN_EMAIL=someone@example.com
        DEFAULT_SUPERADMIN_PASSWORD=<used only to create a missing account>

    Neither has a default. A shipped default password is a published one.
    """
    import os

    email = (os.environ.get("DEFAULT_SUPERADMIN_EMAIL") or "").strip().lower()
    if not email:
        return

    password = os.environ.get("DEFAULT_SUPERADMIN_PASSWORD") or ""
    schema = settings.db_schema

    try:
        sync_url = DBConfig().get_db_url().replace("postgresql+asyncpg://", "postgresql://")
        engine = create_engine(sync_url, echo=False)

        with engine.begin() as conn:
            row = conn.execute(
                text(f'SELECT user_id FROM {schema}."user" WHERE lower(email) = :e'),
                {"e": email},
            ).first()

            if row is None:
                if not password:
                    logger.warning(
                        "DEFAULT_SUPERADMIN_EMAIL names an unknown account and no "
                        "DEFAULT_SUPERADMIN_PASSWORD was given; creating an account "
                        "nobody could sign into would help no one. Nothing done."
                    )
                    return

                # The core's own hasher: a second implementation here would
                # be a second thing to get wrong, and a password hashed
                # differently would simply never match at login.
                from apowerb.helpers.security import get_password_hash

                conn.execute(
                    text(
                        f'INSERT INTO {schema}."user" '
                        "(email, first_name, last_name, password, role, "
                        " email_verified, onboarding_completed) "
                        "VALUES (:e, :fn, :ln, :pw, 'ADMIN', TRUE, FALSE)"
                    ),
                    {
                        "e": email,
                        "fn": "Super",
                        "ln": "Admin",
                        "pw": get_password_hash(password),
                    },
                )
                # email_verified TRUE on purpose: the address comes from the
                # operator who deployed the install, and a verification mail
                # it cannot receive would lock the only way in.
                logger.info("Created the bootstrap administrator account.")
                row = conn.execute(
                    text(f'SELECT user_id FROM {schema}."user" WHERE lower(email) = :e'),
                    {"e": email},
                ).first()
            else:
                # Existing account: promote, never re-key. A bootstrap that
                # reset a password on every restart would be a backdoor.
                conn.execute(
                    text(
                        f'UPDATE {schema}."user" SET role = \'ADMIN\' '
                        "WHERE user_id = :u AND role::text <> 'ADMIN'"
                    ),
                    {"u": row[0]},
                )

            conn.execute(
                text(
                    f"INSERT INTO {schema}.admin_superadmin (user_id) "
                    "VALUES (:u) ON CONFLICT DO NOTHING"
                ),
                {"u": row[0]},
            )
        logger.info("Bootstrap superadministrator ensured.")
    except Exception as exc:  # pragma: no cover - startup must not die here
        # A failure here must not keep the platform down: it costs the
        # control panel, not the product. It is logged loudly instead.
        logger.error("Could not ensure the bootstrap superadministrator: %s", exc)
