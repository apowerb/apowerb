"""Regression tests for the duplicate-notification handling in
``handle_outlook_notification``.

Live regression 2026-05-07 15:34 UTC: every Microsoft Graph
re-delivery for the same ``(subscription_id, resource_id)`` surfaced
as a 500 instead of a 202. The unique-index INSERT correctly raised
``IntegrityError`` — the existing ``except`` caught the commit, but
the next line read ``subscription.id`` from the now-expired ORM
instance, which triggered a lazy-load → implicit flush → the
still-pending ``log`` got re-attempted against the index → second
IntegrityError, this one outside the ``try`` → 500.

Two fixes covered here:
  - ORM attribute values are captured into plain locals BEFORE the
    INSERT.
  - The pending ``log`` is ``expunge``d in the duplicate branch so a
    subsequent autoflush does not re-trigger the INSERT.
"""
from __future__ import annotations

import inspect

from th2agent.routers.webhook_handlers import outlook as handler


def test_handler_captures_subscription_id_locally_before_insert():
    """``subscription.id`` must be assigned to a plain local (e.g.
    ``sub_db_id = subscription.id``) BEFORE the WebhookLog INSERT, so
    the ``except`` block does not need to touch the (then-expired)
    ORM attribute."""
    src = inspect.getsource(handler.handle_outlook_notification)
    sub_id_capture_idx = src.find("sub_db_id = subscription.id")
    assert sub_id_capture_idx >= 0, (
        "ORM attribute ``subscription.id`` must be captured into a local "
        "named ``sub_db_id`` before the WebhookLog INSERT — see live "
        "regression at 2026-05-07 15:34 UTC."
    )

    insert_idx = src.find("log = WebhookLog(")
    assert insert_idx >= 0
    assert sub_id_capture_idx < insert_idx, (
        "The capture must come BEFORE the INSERT, so the ``except`` "
        "branch can rely on the local without re-loading the expired "
        "ORM instance."
    )


def test_handler_uses_local_in_duplicate_log_message():
    """The duplicate-log path must reference the captured locals
    (``sub_db_id`` / ``resource``), NOT ``subscription.id`` /
    ``notification.resource`` (those would trigger the bug)."""
    src = inspect.getsource(handler.handle_outlook_notification)
    # Find the duplicate-skip log line.
    dup_log_idx = src.find("Duplicate notification for sub_db_id=")
    assert dup_log_idx >= 0
    # Look at the next ~200 chars (the args of logger.info).
    snippet = src[dup_log_idx : dup_log_idx + 400]
    assert "sub_db_id, resource" in snippet, (
        "The duplicate-skip logger.info must use the captured locals "
        "(``sub_db_id, resource``), not ``subscription.id`` / "
        "``notification.resource`` — those would re-trigger the "
        "expired-ORM lazy-load that caused the 500."
    )
    # Negative: the offending pattern must NOT reappear.
    assert "subscription.id, notification.resource" not in snippet


def test_handler_expunges_pending_log_on_duplicate():
    """After ``db.rollback()`` the ``log`` object is still pending in
    the session. A subsequent autoflush (e.g. from any ORM read on
    the next loop iteration) would re-attempt the INSERT and re-hit
    the unique index. The duplicate branch must ``db.expunge(log)``
    to detach it."""
    src = inspect.getsource(handler.handle_outlook_notification)
    # The expunge must appear inside the IntegrityError branch.
    except_idx = src.find("except IntegrityError")
    assert except_idx >= 0
    after_except = src[except_idx:]
    # Some defensive form is required.
    assert "expunge(log)" in after_except, (
        "The IntegrityError branch must call ``db.expunge(log)`` so the "
        "still-pending ORM object cannot trigger an autoflush re-INSERT."
    )



def test_handler_captures_log_id_via_flush_before_commit():
    """Live regression 2026-05-19 ~07:00-08:00 UTC: every fresh
    Microsoft Graph notification surfaced as a 500 on
    ``POST /api/webhooks/outlook/notifications``. Root cause: after
    ``await db.commit()``, the ``WebhookLog`` instance is expired
    (SQLAlchemy default ``expire_on_commit=True``). The logger call
    that follows reads ``log.id``, which fires a lazy-load SELECT
    outside the greenlet context → ``MissingGreenlet`` exception.

    Fix: call ``await db.flush()`` BEFORE ``await db.commit()`` so the
    primary key is populated via RETURNING while the greenlet is
    still active, then capture ``log_id = log.id`` into a local
    before the commit expires the instance.

    Microsoft Graph retried the notification 1-2s later and the
    second delivery hit the duplicate branch (no commit on the
    expired instance), so no webhook was lost in the end — but
    every fresh AR produced a 500 on the first delivery.
    """
    src = inspect.getsource(handler.handle_outlook_notification)

    # Scope to the enqueue block; strip comment-only lines so the
    # commentary inside the fix does not confuse the order check.
    add_idx = src.find("db.add(log)")
    except_idx = src.find("except IntegrityError", add_idx)
    raw_block = src[add_idx:except_idx]
    lines = [
        line for line in raw_block.splitlines()
        if not line.lstrip().startswith("#")
    ]
    block = chr(10).join(lines)

    flush_idx = block.find("await db.flush()")
    capture_idx = block.find("log_id = log.id")
    commit_idx = block.find("await db.commit()")
    enqueued_log_idx = block.find('"[OUTLOOK WEBHOOK] Enqueued log_id=%s')

    assert flush_idx >= 0, (
        "``await db.flush()`` must be called BEFORE ``await db.commit()`` "
        "so ``log.id`` is populated while the greenlet is still active — "
        "see live regression 2026-05-19 ~07:00 UTC."
    )
    assert capture_idx >= 0, (
        "``log.id`` must be captured into a local named ``log_id`` BEFORE "
        "the commit expires the ``WebhookLog`` instance."
    )
    assert flush_idx < capture_idx < commit_idx, (
        f"Order must be: ``await db.flush()`` -> ``log_id = log.id`` -> "
        f"``await db.commit()``. Got flush={flush_idx}, "
        f"capture={capture_idx}, commit={commit_idx}."
    )
    # Logger must reference the captured local, not the expired ORM attr.
    logger_call = block[enqueued_log_idx:enqueued_log_idx + 300]
    assert "log.id" not in logger_call, (
        "The Enqueued logger call must use the captured local ``log_id``, "
        "not ``log.id`` (which triggers a lazy-load post-commit)."
    )
    assert "log_id" in logger_call, (
        "The Enqueued logger call must reference the captured local ``log_id``."
    )
