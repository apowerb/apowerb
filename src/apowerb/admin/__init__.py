"""Control panel — users, groups, organisations and permissions.

Lived in a private extension until 18/08/2026, on the terms Farid set on
12/08: "tu la rajoute comme extension que pour nous en interne. On va la
rendre public plus tard." This is that later.

Nothing here was untangled to make the move: the extension only ever used
one core extension point, so publishing it was moving a directory and
calling it directly instead of through the registry.

What it owns: six tables prefixed ``admin_`` -- groups and their members
and permissions, organisations and their members, and the superadmin
grant. What it does NOT own: ``user.role`` and ``UserRole``, which stay
where they were. This adds groups on top of the role; it does not
reimplement it.

Out of scope on purpose: **deleting a user**. Nobody asked for it and it is
irreversible. A group can be deleted, since it owns nothing beyond its own
memberships.

The tables are created by ``ensure_admin_tables()`` from ``bootstrap()``,
never at import time -- importing ``apowerb`` must never touch a database.
"""

from __future__ import annotations

__all__ = ["router", "ensure_admin_tables", "ensure_superadmin_grant", "is_superadmin"]


def __getattr__(name: str):
    # Import paresseux : `apowerb.admin` doit pouvoir etre importe sans
    # tirer FastAPI ni SQLAlchemy dans un contexte qui n'en veut pas.
    if name == "router":
        from apowerb.admin.router import router

        return router
    if name in ("ensure_admin_tables", "ensure_superadmin_grant"):
        from apowerb.admin import migration

        return getattr(migration, name)
    if name == "is_superadmin":
        from apowerb.admin.guard import is_superadmin

        return is_superadmin
    raise AttributeError(name)
