"""Give a fresh install its first administrator.

On a new database nobody is an ADMIN: `role` defaults to USER, and no route
exposes it -- `UserUpdate` deliberately omits the field, because the guard on
`PATCH /api/users/{id}` lets a user edit their own profile and anyone could
otherwise promote themselves. The only other path to ADMIN is the BYPASS_AUTH
developer bypass, which production refuses outright.

So the first administrator had to be written in by hand with SQL. That is fine
on a machine you own and impossible on a hosted deployment, where the operator
often has no database console at all. These two variables are the supported way
in:

    DEFAULT_SUPERADMIN_EMAIL=someone@example.com
    DEFAULT_SUPERADMIN_PASSWORD=<used only to create a missing account>

Neither has a default. A shipped default password is a published one.

The commercial admin brick carries the same bootstrap and additionally records
the account in its own `admin_superadmin` table. Both are idempotent and safe to
run together: whichever goes first creates or promotes, the other finds the work
already done.
"""

import logging
import os

from sqlalchemy import create_engine, func, select, update

from apowerb.helpers.database_connection import DBConfig
from apowerb.models import User, UserRole

logger = logging.getLogger(__name__)


def designated_superadmin_email() -> str:
    """The address the operator named, normalised. Empty when unset."""
    return (os.environ.get("DEFAULT_SUPERADMIN_EMAIL") or "").strip().lower()


def is_designated_superadmin(email: str | None) -> bool:
    """Is this the address the operator named as the first administrator?

    Setting the e-mail alone is the whole configuration: on a new install the
    person it designates has not signed up yet, so whoever signs up with that
    address becomes the administrator, with their own password. A blank value
    designates nobody -- otherwise a stray `DEFAULT_SUPERADMIN_EMAIL=` would
    hand ADMIN to the next person who registers.
    """
    designated = designated_superadmin_email()
    if not designated:
        return False
    return (email or "").strip().lower() == designated


def ensure_default_superadmin(engine=None) -> None:
    """Promote -- or create -- the account named by the environment.

    Runs at every startup and is idempotent. `engine` is injectable so the
    tests can drive it against SQLite; production leaves it out.
    """
    email = designated_superadmin_email()
    if not email:
        return

    password = os.environ.get("DEFAULT_SUPERADMIN_PASSWORD") or ""
    own_engine = engine is None

    try:
        if own_engine:
            sync_url = (
                DBConfig().get_db_url().replace("postgresql+asyncpg://", "postgresql://")
            )
            engine = create_engine(sync_url, echo=False)

        with engine.begin() as conn:
            row = conn.execute(
                select(User.user_id).where(func.lower(User.email) == email)
            ).first()

            if row is None:
                if not password:
                    # Expected, and not a problem: the designated person has
                    # not signed up yet. They are made an administrator the
                    # moment they do -- see `is_designated_superadmin`, used by
                    # user creation. Setting DEFAULT_SUPERADMIN_PASSWORD as
                    # well is only for creating the account up front.
                    logger.info(
                        "Bootstrap administrator '%s' has not signed up yet; "
                        "the account will be created as ADMIN when they do.",
                        email,
                    )
                    return

                # The core's own hasher, never a second implementation: a
                # password hashed differently would simply never match at login.
                from apowerb.helpers.security import get_password_hash

                conn.execute(
                    User.__table__.insert().values(
                        email=email,
                        first_name="Super",
                        last_name="Admin",
                        password=get_password_hash(password),
                        role=UserRole.ADMIN,
                        # Verified on purpose: the address comes from whoever
                        # deployed this install, and a verification mail they
                        # cannot receive would lock the only way in.
                        email_verified=True,
                        onboarding_completed=False,
                    )
                )
                logger.info("Created the bootstrap administrator account.")
            else:
                # Existing account: promote, never re-key. A bootstrap that
                # reset the password on every restart would be a backdoor --
                # anyone holding the variable could take over a live account.
                conn.execute(
                    update(User.__table__)
                    .where(User.user_id == row[0])
                    .values(role=UserRole.ADMIN)
                )
                logger.info("Promoted the bootstrap administrator account.")
    except Exception as exc:  # noqa: BLE001 -- must not keep the platform down
        # Losing the bootstrap administrator costs the control panel, not the
        # product. Loud in the logs, harmless at boot.
        logger.error("Could not ensure the bootstrap superadministrator: %s", exc)
    finally:
        if own_engine and engine is not None:
            engine.dispose()
