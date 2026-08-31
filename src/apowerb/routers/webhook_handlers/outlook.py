"""Outlook (Microsoft Graph) webhook notification handler.

Handles incoming notifications from Microsoft Graph push subscriptions.

The HTTP endpoint only enqueues a row in ``webhook_logs`` (status
``pending``) so Microsoft gets its 202 in <100ms regardless of LLM
load. The actual fetch + agent run happens in
``apowerb.scheduler.backlog_worker``, which drains the queue one row
at a time so concurrent notifications never compound on the Gemini
per-minute input-token quota.
"""

import json
import os
import shutil
import string
from datetime import datetime, timezone
from logging import getLogger

from fastapi import BackgroundTasks, Query, Request, Response

from apowerb.configs.paths import uploads_dir
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from html.parser import HTMLParser
from apowerb.helpers.database import sessionmanager
from apowerb.storage.webhook_attachments import (
    resolve_attachment_path,
    store_webhook_attachment,
)
from apowerb.integrations.outlook_webhook import OutlookWebhookService
from apowerb.models import WebhookLog, WebhookSubscription
from apowerb.schema.webhook_schema import MicrosoftGraphNotificationPayload

from ._common import (
    create_webhook_notification,
    run_agent_for_webhook,
)
from apowerb.core.extensions.registry import registry as _ext_registry


def _fanout_should_split(attachments) -> bool:
    hook = _ext_registry.webhook_hook("fanout.should_split")
    return bool(hook(attachments)) if hook is not None else False


def _fanout_is_split_child(resource_id) -> bool:
    hook = _ext_registry.webhook_hook("fanout.is_split_child")
    return bool(hook(resource_id)) if hook is not None else False


def _fanout_pdf_attachments(attachments) -> list:
    hook = _ext_registry.webhook_hook("fanout.pdf_attachments")
    return hook(attachments) if hook is not None else []


def _fanout_build_child_row_kwargs(parent, pdf_meta, *, idx: int, n: int) -> dict:
    hook = _ext_registry.webhook_hook("fanout.build_child_row_kwargs")
    return hook(parent, pdf_meta, idx=idx, n=n) if hook is not None else {}


async def _emit_run_outcome(**kwargs) -> None:
    hook = _ext_registry.webhook_hook("outcome")
    if hook is not None:
        await hook(**kwargs)


async def _augment_agent_response(
    *, agent_id: int, user_id: int, session_id: str, log_id: int, response: str,
) -> str:
    """Run the generic ``augment_agent_response`` overlay hook, best-effort.

    The hook may return an augmented response string (used as-is) or
    None/empty (original kept). A hook failure NEVER breaks the run —
    the original response is kept and the failure is logged.
    """
    hook = _ext_registry.webhook_hook("augment_agent_response")
    if hook is None:
        return response
    try:
        augmented = await hook(
            agent_id=agent_id, user_id=user_id, session_id=session_id,
            log_id=log_id, response=response,
        )
        return augmented if augmented else response
    except Exception:  # noqa: BLE001 — augmentation must never break the run
        logger.warning(
            "[OUTLOOK WEBHOOK BG] augment_agent_response hook failed "
            "(log_id=%s) — keeping raw response", log_id, exc_info=True,
        )
        return response

logger = getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PlainTextExtractor(HTMLParser):
    """Strip an HTML body down to its visible plain text.

    We use ``html.parser`` from stdlib rather than ``beautifulsoup4``
    on purpose — bs4 is not a project dependency and we don't need
    its full DOM tree to get a searchable string out of an email.
    """

    _IGNORED_TAGS = {"script", "style", "head", "meta", "link"}

    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._IGNORED_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._IGNORED_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        return " ".join(s.strip() for s in self._chunks if s.strip())


def _strip_html(html: str) -> str:
    if not html:
        return ""
    parser = _PlainTextExtractor()
    try:
        parser.feed(html)
    except Exception:  # malformed HTML — return what we have
        pass
    return parser.text()


def _build_agent_message(
    email_data: dict, template: str | None, resource: str = "",
) -> str:
    """Build the agent input message from Outlook email data and an optional template.

    Uses ``string.Template`` with ``$variable`` syntax (safe substitution)
    instead of ``str.format()`` to prevent format-string injection from
    attacker-controlled email fields (subject, sender, body).
    """
    sender_obj = email_data.get("from", {}).get("emailAddress", {})
    sender_name = sender_obj.get("name", "")
    sender_email = sender_obj.get("address", "")
    sender = f"{sender_name} <{sender_email}>" if sender_name else sender_email

    subject = email_data.get("subject", "(no subject)")
    body_preview = email_data.get("bodyPreview", "")
    received = email_data.get("receivedDateTime", "")

    substitutions = dict(
        sender=sender,
        sender_name=sender_name,
        sender_email=sender_email,
        subject=subject,
        body_preview=body_preview,
        received=received,
        resource=resource,
    )

    if template:
        try:
            return string.Template(template).safe_substitute(substitutions)
        except Exception as exc:
            logger.warning(
                "[OUTLOOK WEBHOOK] Template formatting failed (%s), using default", exc
            )

    # Default message
    return (
        f"New email received from {sender}\n"
        f"Subject: {subject}\n"
        f"Received: {received}\n\n"
        f"{body_preview}"
    )


def _extract_email_sender(email_data: dict) -> str:
    """Extract a human-readable sender string from Outlook email data."""
    sender_obj = email_data.get("from", {}).get("emailAddress", {})
    sender_name = sender_obj.get("name", "")
    sender_email = sender_obj.get("address", "")
    if sender_name:
        return f"{sender_name} <{sender_email}>"
    return sender_email


# ---------------------------------------------------------------------------
# Worker callback — fetch the email and run the agent
# ---------------------------------------------------------------------------


def _reader_agent_folders(agent_id, _max_depth: int = 4) -> tuple[list[str], bool]:
    """Folder names to stage into: the triggered agent, then every sub-agent
    below it.

    A webhook triggers ONE agent, but ``tool_pdf_first_page`` is bound by
    ``bind_pdf_first_page`` to the folder of the agent that DECLARES the tool.
    In a sequential pipeline that reader is a sub-agent, so staging only into
    the triggered agent's folder puts the PDF where nobody looks: the reader
    reports an empty ``available_files`` and the run continues without ever
    reading the attachment.

    Returns ``(folder names, complete)``, triggered agent first. ``complete``
    is False when the list is known NOT to cover every reader -- a resolution
    failure at any depth, or a walk truncated by the depth cap. Callers must
    not announce a staged file when it is False: a reader outside the list sees
    an empty uploads dir, which is the whole defect this function exists for.

    A resolution failure falls back to the triggered agent alone: the
    accumulator is local and only adopted once the walk succeeded, so a failure
    halfway can never return a half-explored tree that reads like a complete
    one.
    """
    parent_only = [f"agent{agent_id}"]
    try:
        from apowerb.core.agent_main import agent_store

        folders = list(parent_only)
        seen = {str(agent_id)}
        frontier, depth = [str(agent_id)], 0
        while frontier and depth < _max_depth:
            query = agent_store.agent_table.select().where(
                agent_store.agent_table.c.agent_id.in_(
                    [int(a) for a in frontier if str(a).isdigit()]
                )
            )
            children: list[str] = []
            rows = 0
            with agent_store.engine.begin() as conn:
                for row in conn.execute(query):
                    rows += 1
                    raw = getattr(row, "sub_agents", None)
                    for name in json.loads(raw) if raw else []:
                        num = str(name).replace("agent", "")
                        if num and num not in seen:
                            seen.add(num)
                            children.append(num)
                            folders.append(f"agent{num}")
            asked = len([a for a in frontier if str(a).isdigit()])
            if rows < asked:
                # A missing row is not a leaf: we simply do not know whether
                # that agent has children, and treating silence as "no
                # children" is how an unlisted reader gets a file announced
                # that it never received.
                logger.error(
                    "[OUTLOOK WEBHOOK BG] %d of %d agent row(s) missing while "
                    "walking agent_id=%s -- reader list is INCOMPLETE",
                    asked - rows, asked, agent_id,
                )
                return folders, False
            frontier, depth = children, depth + 1
        if frontier:
            # Truncation means the folder list is NOT the full set of readers,
            # so nothing downstream may treat it as complete.
            logger.error(
                "[OUTLOOK WEBHOOK BG] sub-agent walk of agent_id=%s hit the depth "
                "cap (%d) with %d agent(s) left to visit -- the reader list is "
                "INCOMPLETE", agent_id, _max_depth, len(frontier),
            )
            return folders, False
        return folders, True
    except Exception as exc:  # noqa: BLE001 -- staging must never break a replay
        logger.warning(
            "[OUTLOOK WEBHOOK BG] could not resolve sub-agents of agent_id=%s "
            "(%s) -- staging into its own folder only", agent_id, exc,
        )
        return parent_only, False


def _stage_stored_attachments(stored_attachments, agent_id) -> list[str]:
    """Copy stored attachment PDFs (captured at receipt, on disk) into the
    uploads/ dir of every agent that might read them, so they can be read via
    tool_pdf_first_page WITHOUT re-fetching from Graph. Returns the staged PDF
    filenames."""
    # Relative path, on purpose: matches tool_download_attachment /
    # tool_pdf_first_page which read/write uploads/agent{id} relative to
    # the service CWD (project root). Staging here = where the agent looks.
    folders, complete = _reader_agent_folders(agent_id)
    save_dirs = []
    for folder in folders:
        d = str(uploads_dir() / folder)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as exc:
            # Dropping the folder silently would re-open the defect: the reader
            # living there would find nothing while the file was announced.
            complete = False
            logger.error(
                "[OUTLOOK WEBHOOK BG] cannot create uploads dir %s: %s -- reader "
                "coverage is now incomplete", d, exc,
            )
            continue
        save_dirs.append(d)
    staged: list[str] = []
    for a in stored_attachments or []:
        path = a.get("path")
        fn = a.get("filename")
        if not path or not fn or not os.path.exists(path):
            continue
        if not fn.lower().endswith(".pdf"):
            continue
        base = os.path.basename(fn)
        # Two phases, so a failure never touches what is already on disk: copy
        # every destination to a sibling temp file first, and only move them
        # into place once ALL of them landed. A previous cleanup pass deleted
        # the destination on failure, which could remove a perfectly good copy
        # staged by an earlier attempt.
        pending, failed = [], []
        for save_dir in save_dirs:
            dst = os.path.join(save_dir, base)
            tmp = dst + ".part"
            try:
                shutil.copyfile(path, tmp)
                pending.append((tmp, dst))
            except Exception as exc:  # noqa: BLE001 -- collected, reported below
                failed.append(save_dir)
                logger.warning(
                    "[OUTLOOK WEBHOOK BG] stage attachment %r into %s failed: %s",
                    fn, save_dir, exc,
                )
        # A file is only "staged" once EVERY candidate reader has it, and only
        # when the reader list itself is known to be complete. Announcing a
        # partial copy is how this defect caused wrong verdicts in the first
        # place: the note told the agent the PDF was in its uploads dir, the
        # agent found nothing, and the pipeline carried on regardless.
        if complete and save_dirs and not failed:
            for tmp, dst in pending:
                os.replace(tmp, dst)
            staged.append(base)
            continue
        logger.error(
            "[OUTLOOK WEBHOOK BG] %r NOT announced: %d/%d reader folder(s) failed, "
            "reader list complete=%s", fn, len(failed), len(folders), complete,
        )
        for tmp, _dst in pending:
            try:
                os.remove(tmp)
            except OSError:
                pass
    if staged:
        logger.info(
            "[OUTLOOK WEBHOOK BG] staged %d PDF into %d folder(s): %s",
            len(staged), len(save_dirs), ", ".join(save_dirs),
        )
    return staged


def _replay_instruction(staged_names: list[str]) -> str:
    """Note appended to the agent input in replay mode: the email is gone from
    Graph, so the PDFs are pre-staged and the Graph tools must NOT be used."""
    head = (
        "\n\n--- REPLAY MODE ---\n"
        "This email could NOT be re-fetched from Outlook (it left the mailbox). "
        "Its subject and body are above. Do NOT call tool_read_email or "
        "tool_download_attachment (they will fail with 404). "
    )
    if not staged_names:
        # Never claim a file the agent cannot open: an agent told the PDF is
        # there, finding nothing, still produces an answer -- and a confident
        # answer with no document read is exactly the failure to avoid.
        return head + (
            "NO attachment could be staged for you. Do NOT assume a PDF is "
            "available and do NOT infer its contents: report that the document "
            "could not be read rather than answering from the subject alone."
        )
    return head + (
        "Its PDF attachment(s) are ALREADY in your uploads directory: "
        f"{', '.join(staged_names)}. Call basic.tool_pdf_first_page(<filename>) "
        "directly on the candidate AR PDF."
    )


async def _split_into_children(
    db,
    log_id: int,
    stored_attachments: list[dict],
    parent_snap: dict,
) -> list[int]:
    """Create one child WebhookLog row per PDF attachment and mark the parent success.

    ``parent_snap`` MUST be a plain dict of scalar fields captured BEFORE any
    ``await db.commit()`` call (SQLAlchemy expire_on_commit=True would otherwise
    expire every attribute on the ORM instance, causing MissingGreenletError when
    read from the fan-out loop — CRITICAL 2 fix, 2026-05-29).

    Returns the list of created child_ids. The caller is responsible for checking
    ``should_split(stored_attachments)`` before calling this helper; calling it
    with fewer than 2 PDFs is harmless (it returns an empty list) but wasteful.

    The parent row is marked ``status='success'`` with a ``[SPLIT]`` message
    via a fresh db2 session (same pattern as the original inline code) so the
    commit does not interfere with the current ``db`` session's open transaction.
    """
    pdfs = _fanout_pdf_attachments(stored_attachments)
    n = len(pdfs)
    child_ids: list[int] = []

    for idx, pdf_meta in enumerate(pdfs):
        # CRITICAL 2: pass the pre-commit snapshot dict, not the expired ORM instance.
        child_kwargs = _fanout_build_child_row_kwargs(parent_snap, pdf_meta, idx=idx, n=n)
        child_resource_id = child_kwargs["resource_id"]

        # Idempotency: skip if child already exists.
        existing = await db.execute(
            select(WebhookLog).where(
                WebhookLog.subscription_id == child_kwargs["subscription_id"],
                WebhookLog.resource_id == child_resource_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            logger.info(
                "[FANOUT] log_id=%s child idx=%d already exists (%s) — skip",
                log_id, idx, child_resource_id,
            )
            continue

        child = WebhookLog(**child_kwargs)
        db.add(child)
        try:
            await db.flush()
            child_id = child.id
            # Copy the single PDF bytes from the parent's stored path.
            try:
                pdf_path = resolve_attachment_path(log_id, pdf_meta["filename"])
                pdf_bytes = pdf_path.read_bytes()
                stored_child = store_webhook_attachment(
                    log_id=child_id,
                    filename=pdf_meta["filename"],
                    content=pdf_bytes,
                    content_type=pdf_meta.get("content_type"),
                )
                child.attachments = [stored_child.to_jsonable()]
            except Exception as exc_pdf:  # noqa: BLE001
                # MINOR: log ERROR and SKIP this child rather than
                # persisting a row with the parent's path as
                # attachment (dead reference after the parent is
                # archived / purged). A missing child is visible and
                # retryable; a child with a stale path silently fails.
                logger.error(
                    "[FANOUT] copy PDF failed for %r (log_id=%s child_id=%s)"
                    " — child NOT created: %s",
                    pdf_meta.get("filename"), log_id, child_id, exc_pdf,
                )
                await db.rollback()
                try:
                    db.expunge(child)
                except Exception:
                    pass
                continue
            await db.commit()
            child_ids.append(child_id)
            logger.info(
                "[FANOUT] log_id=%s created child_id=%s for PDF %r",
                log_id, child_id, pdf_meta.get("filename"),
            )
        except IntegrityError:
            await db.rollback()
            try:
                db.expunge(child)
            except Exception:
                pass
            logger.info(
                "[FANOUT] log_id=%s child %r already exists (IntegrityError) — skip",
                log_id, child_resource_id,
            )

    # Mark parent as split-success and bail out via a fresh session so the
    # commit does not interfere with the caller's open db session state.
    async with sessionmanager.session() as db2:
        parent_row = await db2.get(WebhookLog, log_id)
        if parent_row:
            parent_row.status = "success"
            parent_row.agent_response = (
                f"[SPLIT] split into {n} children: {child_ids}"
            )
            await db2.commit()

    logger.info(
        "[FANOUT] log_id=%s split complete — %d children created: %s",
        log_id, len(child_ids), child_ids,
    )
    return child_ids


async def process_webhook_log_row(log_id: int) -> str | None:
    """Worker callback: fetch the Outlook email and run the agent.

    Receives the webhook ``log_id`` (a SCALAR). The worker (``_claim_one``)
    deliberately no longer hands out a ``WebhookLog`` ORM instance: a detached
    instance crossing the session boundary raises ``DetachedInstanceError`` on
    any lazy-refresh (incident 2026-05-29). The row is loaded fresh, in-session,
    here. The row has already been claimed by the worker (``status='in_progress'``,
    ``attempts`` incremented, ``started_at`` stamped).

    Re-raises any exception (including :class:`RateLimitError`) so the
    worker can decide between requeue-with-backoff and permanent
    failure.
    """
    async with sessionmanager.session() as db:
        _claimed = await db.get(WebhookLog, log_id)
        if _claimed is None:
            raise RuntimeError(f"log_id={log_id}: WebhookLog introuvable (supprime ?)")
        sub_db_id = _claimed.subscription_id
        user_id = _claimed.user_id
        agent_id = _claimed.agent_id
        notification_resource = _claimed.resource_id or ""
        # Email reception time (PR #273 / the overlay's reception date). Capture the
        # scalar WHILE _claimed is attached; the overlay maps it to
        # email_received_at via the initial_state_extras hook (generic core
        # just forwards the timestamp in the hook ctx).
        _email_received_at = getattr(_claimed, "created_at", None)
        _is_child = _fanout_is_split_child(notification_resource)

    logger.info(
        "[OUTLOOK WEBHOOK BG] Processing log_id=%s sub_db_id=%s resource=%s is_child=%s",
        log_id, sub_db_id, notification_resource, _is_child,
    )

    # Values needed for initial_state, which is built AFTER the session below
    # closes. Capture them as plain locals WHILE the ORM instance is attached
    # (and before any commit expires it) — never read row/log_row attributes
    # once detached, or SQLAlchemy raises DetachedInstanceError on lazy-refresh
    # (live 2026-05-29, log_id 4634/4635 looped in the backlog after a restart).
    _force_reprocess = False
    _email_body_text = ""

    async with sessionmanager.session() as db:
        # 1. Get fresh access token for the subscription owner
        # (not needed for fan-out child rows — they go straight to replay)
        fetch_exc = None

        if _is_child:
            # Fan-out child: skip Graph entirely. The single PDF was already
            # stored under this child log_id when the parent was split.
            # Force the replay branch below to read attachments from disk.
            email_data = None
            fetch_exc = RuntimeError(
                f"log_id={log_id}: fan-out child row — Graph skipped, using stored PDF"
            )
            logger.info(
                "[OUTLOOK WEBHOOK BG] log_id=%s is a split child — forced replay path",
                log_id,
            )
        else:
            access_token = await OutlookWebhookService.get_access_token_for_user(
                db, user_id
            )

            # 2. Fetch the email content. If it left the mailbox (Graph 404),
            # fall back to the copy captured at receipt (body + PDF on disk) so
            # the AR can still be (re)processed -- completes the replay design.
            try:
                email_data = await OutlookWebhookService.fetch_email(
                    access_token, notification_resource
                )
            except Exception as exc:  # noqa: BLE001
                # Only fall back to the stored copy when the email genuinely left
                # the mailbox (Graph 404 / ErrorItemNotFound). Any other failure
                # (401 token expired, 403, 429, 5xx) MUST propagate so the worker
                # requeues/backs off and the integration breakage is not masked.
                _m = str(exc)
                if "404" not in _m and "ErrorItemNotFound" not in _m:
                    raise
                email_data = None
                fetch_exc = exc

        if email_data is not None:
            # 3. Build agent message from the freshly-fetched email + template
            sub_row = await db.get(WebhookSubscription, sub_db_id)
            agent_message_template = sub_row.agent_message_template if sub_row else None
            message_text = _build_agent_message(
                email_data, agent_message_template, resource=notification_resource,
            )
            sender_str = _extract_email_sender(email_data)
            email_subject = email_data.get("subject", "") or ""

            # 3a. Capture body + attachments now, while the email still exists.
            body_obj = (email_data or {}).get("body") or {}
            body_content = body_obj.get("content") or ""
            body_type = (body_obj.get("contentType") or "").lower()
            if body_type == "html":
                body_html_val = body_content
                body_text_val = _strip_html(body_content)
            else:
                body_html_val = None
                body_text_val = body_content

            stored_attachments: list[dict] = []
            if email_data.get("hasAttachments"):
                try:
                    graph_attachments = await OutlookWebhookService.fetch_attachments(
                        access_token, notification_resource,
                    )
                except Exception as exc:  # noqa: BLE001 -- log and continue
                    logger.warning(
                        "[OUTLOOK WEBHOOK BG] log_id=%s fetch_attachments failed: %s",
                        log_id, exc,
                    )
                    graph_attachments = []
                for att in graph_attachments:
                    try:
                        stored = store_webhook_attachment(
                            log_id=log_id,
                            filename=att["name"],
                            content=att["content"],
                            content_type=att.get("contentType"),
                        )
                    except Exception as exc:  # noqa: BLE001 -- log and continue
                        logger.warning(
                            "[OUTLOOK WEBHOOK BG] log_id=%s store_attachment failed for %r: %s",
                            log_id, att.get("name"), exc,
                        )
                        continue
                    stored_attachments.append(stored.to_jsonable())

            # Stamp the log with email metadata + agent_message + captured
            # body+PJ before the agent run -- observability and recovery.
            log_row = await db.get(WebhookLog, log_id)
            if log_row:
                log_row.agent_message = message_text
                log_row.email_subject = email_subject[:500] or None
                log_row.email_sender = sender_str[:500] or None
                log_row.email_body_html = body_html_val or None
                log_row.email_body_text = body_text_val or None
                log_row.attachments = stored_attachments or None
                # Capture BEFORE commit (commit expires log_row's attributes).
                # getattr is safe here (log_row attached -> no detached refresh)
                # and defensive against a missing attribute.
                _force_reprocess = bool(getattr(log_row, "force_reprocess", False))
                _email_body_text = body_text_val or ""
                # CRITICAL 2: snapshot all scalar fields needed by
                # build_child_row_kwargs into a plain dict NOW, while the ORM
                # instance is still attached and un-expired.
                # After `await db.commit()` below, SQLAlchemy's default
                # expire_on_commit=True expires every attribute; reading
                # log_row.subscription_id from the fan-out loop would trigger
                # a refresh outside the greenlet → MissingGreenletError.
                _parent_snap: dict = {
                    "subscription_id": log_row.subscription_id,
                    "user_id": log_row.user_id,
                    "agent_id": log_row.agent_id,
                    "resource_id": log_row.resource_id or "",
                    "trigger_event": log_row.trigger_event or "created",
                    "force_reprocess": bool(log_row.force_reprocess),
                    "email_sender": log_row.email_sender,
                    "email_body_html": body_html_val or None,
                    "email_body_text": body_text_val or None,
                    "agent_message": message_text,
                    "email_subject": email_subject[:500] or None,
                }
                await db.commit()

                # Fan-out: if the email has >= 2 PDF attachments, create one
                # child webhook_logs row per PDF so each is processed by the
                # existing mono-PDF pipeline. The parent row is then marked
                # success immediately (no agent run on the parent).
                if _fanout_should_split(stored_attachments):
                    child_ids = await _split_into_children(
                        db, log_id, stored_attachments, _parent_snap
                    )
                    return f"[SPLIT] parent log_id={log_id} split into {child_ids}"
        else:
            # 3-replay. Email gone from Graph -- reuse the copy captured at
            # receipt (agent_message + body + PDFs already on disk). Stage the
            # PDFs into uploads/ and tell the agent to read them directly.
            log_row = await db.get(WebhookLog, log_id)
            # Capture state values while attached (replay path has no commit,
            # but log_row is still detached once this block exits).
            _force_reprocess = bool(getattr(log_row, "force_reprocess", False)) if log_row else False
            _email_body_text = (getattr(log_row, "email_body_text", "") or "") if log_row else ""
            # attachments is a SQLAlchemy JSON column -> already a Python
            # list when read via the ORM. Tolerate a raw str just in case.
            _att = log_row.attachments if log_row else None
            if isinstance(_att, str):
                stored_attachments = json.loads(_att) if _att else []
            else:
                stored_attachments = _att or []
            base_msg = (log_row.agent_message if log_row else None) or ""
            if not base_msg and not stored_attachments:
                raise RuntimeError(
                    f"log_id={log_id}: Graph fetch failed ({fetch_exc}) and no "
                    "stored copy to replay from"
                ) from fetch_exc

            # Fan-out replay: a parent mail with >= 2 PDFs that was re-queued
            # (e.g. force_reprocess or backlog retry after >48h) must also split
            # so each PDF gets its own AR run — same as the live fetch path.
            # Guard: a split CHILD (resource_id contains __pdf<N>) must NEVER
            # re-split (it carries exactly 1 PDF by design, and the guard prevents
            # any accidental recursive fan-out even if stored_attachments were
            # populated with > 1 entry).
            if not _fanout_is_split_child(notification_resource) and _fanout_should_split(stored_attachments):
                # Capture _parent_snap from log_row while it is still attached
                # (no commit happened in the replay branch, but we capture scalars
                # defensively to reuse the same helper contract as the fetch path).
                _parent_snap_replay: dict = {
                    "subscription_id": log_row.subscription_id if log_row else sub_db_id,
                    "user_id": log_row.user_id if log_row else user_id,
                    "agent_id": log_row.agent_id if log_row else agent_id,
                    "resource_id": (log_row.resource_id or "") if log_row else notification_resource,
                    "trigger_event": (log_row.trigger_event or "created") if log_row else "created",
                    "force_reprocess": bool(log_row.force_reprocess) if log_row else False,
                    "email_sender": (log_row.email_sender or "") if log_row else "",
                    "email_body_html": (log_row.email_body_html or None) if log_row else None,
                    "email_body_text": (log_row.email_body_text or None) if log_row else None,
                    "agent_message": (log_row.agent_message or "") if log_row else "",
                    "email_subject": (log_row.email_subject or None) if log_row else None,
                }
                child_ids = await _split_into_children(
                    db, log_id, stored_attachments, _parent_snap_replay
                )
                logger.info(
                    "[FANOUT REPLAY] log_id=%s replay-split complete — %d children: %s",
                    log_id, len(child_ids), child_ids,
                )
                return f"[SPLIT] parent log_id={log_id} replay-split into {child_ids}"

            staged = _stage_stored_attachments(stored_attachments, agent_id)
            # If the email carried PDFs and none could be staged, failing here
            # is the only mechanism that produces a retry. Continuing would
            # leave the outcome to the model: told no document is available, it
            # may still answer from the subject line, and that answer is
            # persisted and mailed exactly like a real one.
            _pdfs = [
                a for a in (stored_attachments or [])
                if str(a.get("filename") or "").lower().endswith(".pdf")
            ]
            if _pdfs and not staged:
                raise RuntimeError(
                    f"replay of log_id={log_id} carried {len(_pdfs)} PDF but none "
                    "could be staged where the reading agent looks -- refusing to "
                    "run the pipeline on an unread document"
                )
            if not base_msg:
                base_msg = (
                    f"Subject: {(log_row.email_subject or '') if log_row else ''}\n\n"
                    f"{(log_row.email_body_text or '') if log_row else ''}"
                )
            message_text = base_msg + _replay_instruction(staged)
            email_subject = (log_row.email_subject or "") if log_row else ""
            sender_str = (log_row.email_sender or "") if log_row else ""
            logger.info(
                "[OUTLOOK WEBHOOK BG] log_id=%s Graph fetch failed (%s) -- REPLAY "
                "from stored copy (%d staged PDF)", log_id, fetch_exc, len(staged),
            )

    # 4. Run the agent — one fresh ADK session per webhook event.
    # Pinning the session to log_id keeps each AR isolated: no stale
    # state from previous emails, no ContextWindowExceeded from
    # unbounded history accumulation in a shared session.
    # Note: we drop the DB session before the call because the agent
    # run can take minutes and we don't want to hold a connection idle.
    session_id = f"webhook_{log_id}"
    # Fan-out child runs may need extra initial_state keys (e.g. an overlay
    # suppresses the buyer mail for a split child). Supplied by the overlay
    # via the "initial_state_extras" hook; empty dict for the generic core.
    _extras_hook = _ext_registry.webhook_hook("initial_state_extras")
    _initial_state_extras = (
        _extras_hook({"is_split_child": _is_child, "email_received_at": _email_received_at})
        if _extras_hook is not None
        else {}
    )
    try:
        agent_response_text = await run_agent_for_webhook(
            user_id=user_id,
            agent_id=agent_id,
            sub_db_id=sub_db_id,
            session_id=session_id,
            message_text=message_text,
            # Outlook : session UNIQUE par webhook (webhook_<log_id>) -> session
            # fraiche a chaque run (delete-then-create). Cf _common.py.
            fresh_session=True,
            # Tag the ADK session state so the overlay's persist tool
            # can stamp the AR row with the originating webhook_log id.
            # No-op for other agents (they don't read this key).
            initial_state={
                "webhook_log_id": log_id,
                # Deliberate re-processing flag (operator retrigger) so the
                # recorder replaces the existing AR instead of no-op.
                # From a LOCAL captured in-session (see top): NEVER read
                # row/log_row here — both are detached once the session above
                # closed (and log_row is also expired by its commit), so a
                # lazy-refresh raises DetachedInstanceError (which getattr's
                # default does NOT catch — InvalidRequestError, not
                # AttributeError). That crash aborted the run before it started
                # and looped the webhook in the backlog (live 2026-05-29,
                # log_id 4634/4635 after a restart).
                "force_reprocess": _force_reprocess,
                # Email sender for the excluded-supplier gate.
                # Available in both live (~l.245) and replay (~l.324) branches.
                "email_sender": sender_str,
                # Subject + body for the AR/NOT_AR gate (couche 1 objet/corps =
                # le signal le plus discriminant). Sans eux le gate serait
                # aveugle en prod (cf review 2026-05-29). Non injectes au prompt
                # LLM (pas de {key}), seulement lus par le gate.
                "email_subject": email_subject or "",
                "email_body_text": _email_body_text,
                # Slim attachment metadata for the intake's deterministic
                # PDF gate (before_agent_callback). No bytes/paths leaked.
                "attachments": [
                    {
                        "filename": a.get("filename"),
                        "content_type": a.get("content_type"),
                    }
                    for a in stored_attachments
                ],
                # Overlay-supplied extras (e.g. an overlay fan-out child suppresses
                # the buyer mail: the AR is still recorded — recorder runs BEFORE
                # notifier — but the overlay mail tool redirects to the internal
                # address, avoiding N mails for one email carrying N PDFs).
                **_initial_state_extras,
            },
        )
    except Exception:
        # Crash path (e.g. a sentinel that 500s before the recorder): the run
        # is already surfaced as status='error', but classify + LOUD-log
        # the overlay outcome so a silent drop is countable. Never masks the error.
        await _emit_run_outcome(
            agent_id=agent_id, user_id=user_id,
            session_id=session_id, log_id=log_id, crashed=True,
        )
        raise

    # Success path: a 'completed' run can still have silently dropped an AR
    # (sentinel skip / matched-but-not-persisted). Classify + surface anomalies.
    await _emit_run_outcome(
        agent_id=agent_id, user_id=user_id,
        session_id=session_id, log_id=log_id, crashed=False,
    )

    # Post-run response augmentation (overlay hook). The displayed
    # agent_response is the LLM's free text — tool functionResponses are
    # filtered out by extract_agent_response and the LLM cannot be trusted
    # to echo them. An overlay can append a DETERMINISTIC block here (e.g.
    # the overlay's [RESEND_DIFF], incident 2026-07-15) before the worker persists
    # the response. Best-effort: a hook failure never breaks the run.
    agent_response_text = await _augment_agent_response(
        agent_id=agent_id, user_id=user_id, session_id=session_id,
        log_id=log_id, response=agent_response_text,
    )

    # 5. Update last_notification_at on the subscription so the UI
    # reflects activity.
    async with sessionmanager.session() as db:
        sub_row = await db.get(WebhookSubscription, sub_db_id)
        if sub_row:
            sub_row.last_notification_at = datetime.now(timezone.utc)
            await db.commit()

    # 6. Push SSE notification to the operator.
    agent_folder = f"agent{agent_id}"
    await create_webhook_notification(
        user_id,
        title="New email processed",
        message=f"Agent processed email \"{email_subject}\" from {sender_str}",
        link=f"/webhooks?log={log_id}",
        metadata={
            "agent_id": agent_id,
            "email_subject": email_subject,
            "email_sender": sender_str,
            "webhook_log_id": log_id,
            "provider": "microsoft_outlook",
        },
    )

    logger.info(
        "[OUTLOOK WEBHOOK BG] Agent %s triggered successfully for log_id=%s sub_db_id=%s",
        agent_folder, log_id, sub_db_id,
    )
    return agent_response_text


# ---------------------------------------------------------------------------
# Public handler called by the dispatcher — enqueue only
# ---------------------------------------------------------------------------


async def handle_outlook_notification(
    request: Request,
    background_tasks: BackgroundTasks,  # noqa: ARG001 — required by the
    # generic router dispatcher in routers/webhooks.py:721 which passes
    # background_tasks positionally to every provider handler. The
    # backlog worker introduced in PR #120 makes this argument unused
    # at runtime, but removing it from the signature triggered a
    # TypeError on every Graph notification. Keep the slot.
    validationToken: str | None = Query(default=None),
) -> Response:
    """Microsoft Graph notification receiver.

    **Public endpoint** — Microsoft Graph posts directly without our
    auth middleware. The handler does the bare minimum so Microsoft
    gets its 202 in <100 ms even when the LLM tier is saturated:

    1. **Validation handshake** — echo ``validationToken`` as plain text.
    2. **Notification delivery** — verify ``clientState``, then INSERT a
       ``webhook_logs`` row in ``pending`` state. The actual fetch +
       agent run is performed by
       :func:`apowerb.scheduler.backlog_worker.process_once`, which
       drains the queue one row at a time so concurrent notifications
       can never compound on the per-minute LLM input-token quota.

    Idempotent on Graph re-deliveries: a unique index on
    ``(subscription_id, resource_id)`` silently ignores duplicate
    INSERTs so the agent only runs once per message.
    """
    if validationToken is not None:
        logger.info(
            "[OUTLOOK WEBHOOK] Validation handshake received (token length=%d)",
            len(validationToken),
        )
        return PlainTextResponse(content=validationToken, status_code=200)

    try:
        raw_body = await request.json()
        payload = MicrosoftGraphNotificationPayload(**raw_body)
    except Exception as exc:
        logger.error("[OUTLOOK WEBHOOK] Failed to parse notification payload: %s", exc)
        return Response(status_code=202)

    logger.info(
        "[OUTLOOK WEBHOOK] Received %d notification(s) from Microsoft Graph",
        len(payload.value),
    )

    enqueued = 0
    skipped_duplicates = 0

    async with sessionmanager.session() as db:
        for notification in payload.value:
            result = await db.execute(
                select(WebhookSubscription).where(
                    WebhookSubscription.subscription_id == notification.subscriptionId,
                    WebhookSubscription.status == "active",
                )
            )
            subscription = result.scalar_one_or_none()

            if not subscription:
                logger.warning(
                    "[OUTLOOK WEBHOOK] Unknown or inactive subscription_id=%s, skipping",
                    notification.subscriptionId,
                )
                continue

            if not notification.clientState:
                logger.warning(
                    "[OUTLOOK WEBHOOK] Missing clientState for subscription_id=%s, skipping",
                    notification.subscriptionId,
                )
                continue

            if notification.clientState != subscription.client_state:
                logger.warning(
                    "[OUTLOOK WEBHOOK] client_state MISMATCH for subscription_id=%s "
                    "(expected=%s, got=%s). Possible spoofing — skipping.",
                    notification.subscriptionId,
                    subscription.client_state,
                    notification.clientState,
                )
                continue

            # Capture ORM attribute values into plain locals BEFORE the
            # try/except. After ``db.rollback()`` the ``subscription``
            # instance is expired; reading ``subscription.id`` from the
            # ``except`` block triggers a lazy-load → implicit flush →
            # the still-pending ``log`` we just added gets re-attempted
            # against the unique constraint → second IntegrityError that
            # is no longer inside the try. Live regression 2026-05-07
            # 15:34 UTC: every Microsoft Graph re-delivery surfaced as
            # a 500 instead of being silently skipped.
            sub_db_id = subscription.id
            sub_user_id = subscription.user_id
            sub_agent_id = subscription.agent_id
            resource = notification.resource

            log = WebhookLog(
                user_id=sub_user_id,
                subscription_id=sub_db_id,
                agent_id=sub_agent_id,
                trigger_event=notification.changeType or "created",
                resource_id=resource,
                payload_json=json.dumps(
                    {
                        "subscriptionId": notification.subscriptionId,
                        "changeType": notification.changeType,
                        "resource": resource,
                    }
                ),
                status=WebhookLog.STATUS_PENDING,
            )
            db.add(log)
            try:
                # Capture log.id BEFORE await db.commit() returns.
                # SQLAlchemy default expire_on_commit=True expires
                # every attribute on the instance once the commit
                # completes; reading log.id after that point fires
                # a lazy SELECT outside the greenlet → MissingGreenlet.
                # await db.flush() populates the primary key via
                # RETURNING while the greenlet is still active.
                await db.flush()
                log_id = log.id
                await db.commit()
                enqueued += 1
                logger.info(
                    "[OUTLOOK WEBHOOK] Enqueued log_id=%s for sub_db_id=%s",
                    log_id, sub_db_id,
                )
            except IntegrityError:
                # Microsoft Graph re-delivery on the same message —
                # the unique (subscription_id, resource_id) index says
                # we already enqueued this one. Drop it silently.
                await db.rollback()
                # The just-added ``log`` survives ``rollback`` as a
                # pending object that would auto-flush on the next
                # session activity. Expunge it so the next loop
                # iteration does not re-trigger the duplicate INSERT.
                try:
                    db.expunge(log)
                except Exception:
                    pass
                skipped_duplicates += 1
                logger.info(
                    "[OUTLOOK WEBHOOK] Duplicate notification for sub_db_id=%s "
                    "resource=%s — skipped",
                    sub_db_id, resource,
                )

    logger.info(
        "[OUTLOOK WEBHOOK] enqueued=%d skipped_duplicates=%d",
        enqueued, skipped_duplicates,
    )
    return Response(status_code=202)
