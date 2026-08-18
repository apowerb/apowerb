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


def ensure_default_superadmin(engine=None) -> None:
    """Promote -- or create -- the account named by the environment.

    Runs at every startup and is idempotent. `engine` is injectable so the
    tests can drive it against SQLite; production leaves it out.
    """
    email = (os.environ.get("DEFAULT_SUPERADMIN_EMAIL") or "").strip().lower()
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
                    logger.warning(
                        "DEFAULT_SUPERADMIN_EMAIL names an unknown account and no "
                        "DEFAULT_SUPERADMIN_PASSWORD was given; creating an account "
                        "nobody could sign into would help no one. Nothing done."
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
