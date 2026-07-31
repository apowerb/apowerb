"""Chat action-card tools.

Each function in this module is an ADK tool the agent can call to push an
interactive card into the chat UI. The return dicts follow the contract in
``scratchpad/action-cards-contract.md``:

- ``_action_card: True``           — flag the frontend intercepts
- ``kind: "<kind>"``                — routing key for the React card
- ``status: "<kind>_pending"``      — lifecycle state before user responds
- plus all the tool arguments recopied verbatim

The ADK LLM layer uses these docstrings to describe the tools to the model,
so keep ``Args``/``Returns`` sections descriptive and current.
"""

from __future__ import annotations


VALID_USER_INPUT_TYPES = {"text", "number", "select", "multiline", "date", "chips"}


def _request_pause(tool_context) -> None:
    """Termine l'invocation ADK apres l'emission d'une carte interactive.

    Sans cela, le tool ne fait que renvoyer un dict ``*_pending`` au modele,
    qui poursuit le run et finit par re-demander la meme chose en boucle
    jusqu'au plafond ``max_llm_calls`` (bug "envoi de mail demande objet/
    contenu en boucle, stop a 25"). ``escalate`` stoppe un LoopAgent ;
    ``skip_summarization`` stoppe le LlmFlow d'un agent simple
    (``is_final_response`` devient True). Les deux ensemble : le run
    s'arrete, la carte est rendue, l'utilisateur reprend la main au
    tour suivant.

    ADK injecte ``tool_context`` par NOM de parametre ; il vaut None dans les
    tests / appels hors-ADK, auquel cas c'est un no-op.
    """
    if tool_context is None:
        return
    try:
        tool_context.actions.escalate = True
        tool_context.actions.skip_summarization = True
    except Exception:
        pass


# Prepended to every agent instruction so the LLM knows when to use the
# interactive ``request_user_input`` tool (instead of listing options as
# markdown bullets/numbered items in its reply).
#
# IMPORTANT: this string is passed through ADK's instruction templating which
# interprets single braces as session-state placeholders. Do NOT include any
# literal "{foo}" / "{{foo}}" tokens — use angle brackets "<foo>" in examples
# and tell the LLM that the real template syntax in campaign rendering is
# double braces.
INTERACTIVE_UI_INSTRUCTION = """

## Interactive UI — use action cards, never markdown lists of choices

When you want the user to pick from a short list of options (2-7 choices), do
NOT list them as a markdown bullet or numbered list in your text. Instead
call the ``request_user_input`` tool with ``input_type="chips"`` and the list
of choices. The frontend renders clickable chips plus a free-text input so
the user can override with their own answer if none match.

Trigger this pattern whenever you would otherwise write phrases like:
- "Here are a few options..."
- "Which one do you prefer?"
- "Tell me if you'd rather go with A, B or C"
- "Voici X propositions..."

### Suggested actions at the end of a reply

The same rule applies when you close a reply by proposing next steps —
phrases like "Actions suggérées :", "What would you like to do next?",
"Voulez-vous… ?", "Souhaitez-vous… ?", or any bullet / numbered list of
follow-up questions. Never emit these as markdown bullets. Instead call
``request_user_input`` ONCE with ``input_type="chips"``, a short question
(e.g. "Et ensuite ?" / "What's next?") and each suggested action as a chip
label that reads like a direct command the user would click ("Lancer
l'enrichissement HubSpot", "Préparer un draft de campagne", "Rien pour
l'instant"). The chip label becomes the user's reply when they click it, so
phrase it as an instruction, not as a question.

Example call (pseudocode):

    request_user_input(
        question="Which subject line do you prefer?",
        input_type="chips",
        choices=[
            "Hi <firstname>, a quick idea",
            "A better <company> this quarter",
            "3 tips for <topic>",
        ],
        placeholder="Or type your own subject...",
    )

If the downstream email campaign uses double-brace variables like
``<firstname>`` or ``<company>``, keep that exact double-brace syntax (two
opening and two closing braces) in the chip strings you actually send — the
angle brackets above are only used in THIS documentation to avoid confusing
the agent runtime's own placeholder parser.

Rules:
- The card replaces the list — never duplicate the options as markdown in
  the same turn.
- Keep each chip short (under ~60 characters) so it fits as a clickable
  button.
- Include a ``placeholder`` that makes the free-text fallback obvious.
- For a single yes/no confirmation, prefer ``confirm_destructive`` (or plain
  text) — chips are for real choices between multiple alternatives.

## Dashboard context — read before answering

When the user is chatting from a BI dashboard, the runtime sets
``AGENT_DASHBOARD_ID`` in the environment. In that situation:

- If the user asks about the dashboard's content ("what's in this
  dashboard?", "how many rows in the chart?", "summarise the data", "quel
  est le statut des envois?", etc.), call ``tool_get_dashboard_data`` FIRST
  (no ``dashboard_id`` argument needed — it auto-resolves the context) and
  base your answer on the returned ``components`` (labels + rows + KPIs).
- Never fabricate numbers, trends, or counts for a dashboard you have not
  read in the current turn.
- If ``tool_get_dashboard_data`` returns ``success: false`` with an
  AGENT_DASHBOARD_ID error, tell the user the chat is not linked to a
  dashboard and ask them to pass the UUID explicitly.
- To MODIFY this dashboard (add a chart, add a KPI), the modification tools
  (``tool_add_chart_to_dashboard``, ``tool_add_kpi_to_dashboard``) target THIS
  dashboard automatically — do NOT pass a dashboard_id and do NOT ask the user
  which dashboard. Build the chart first (tool_create_chart) or compute the KPI
  value (tool_text_to_sql), then add it.
- You can also answer ANY data question and run normal actions here
  (tool_text_to_sql, charts, exports) — you are a full data agent, not limited
  to read-only dashboard queries.
"""


def request_user_input(
    question: str,
    input_type: str,
    choices: list[str] | None = None,
    placeholder: str | None = None,
    tool_context=None,
) -> dict:
    """Ask the user for a single piece of input via an interactive card.

    Use this when the agent needs a specific answer to continue (name,
    number, date, choice in a list, free text, ...). The frontend renders
    the right widget based on ``input_type``.

    Args:
        question: The prompt shown to the user above the input widget.
        input_type: Widget type. Must be one of: ``text``, ``number``,
            ``select``, ``multiline``, ``date``, ``chips``. Prefer ``chips``
            over ``select`` when you propose a short list of suggestions
            the user can either click directly or override with free text.
        choices: For ``select`` and ``chips``, the list of options to show.
            With ``chips``, the user can also type a free-text answer
            alongside the suggested chips.
        placeholder: Optional placeholder text for free-form inputs.

    Returns:
        dict payload describing the card. If ``input_type`` is invalid,
        returns ``{"status": "error", "message": ...}`` instead.
    """
    if input_type not in VALID_USER_INPUT_TYPES:
        return {
            "status": "error",
            "message": (
                f"Invalid input_type: {input_type!r}. "
                f"Valid: {', '.join(sorted(VALID_USER_INPUT_TYPES))}"
            ),
        }
    _request_pause(tool_context)
    return {
        "_action_card": True,
        "kind": "user_input",
        "status": "user_input_pending",
        "question": question,
        "input_type": input_type,
        "choices": choices,
        "placeholder": placeholder,
    }


def confirm_destructive(
    action: str,
    impact: str,
    item: str | None = None,
    tool_context=None,
) -> dict:
    """Ask the user to confirm a destructive or irreversible action.

    Use this before deleting data, sending emails on the user's behalf,
    or any operation the user cannot undo.

    Args:
        action: Short name of the action (e.g. ``delete_file``).
        impact: Human-readable description of the consequences.
        item: Optional label of the specific item at stake.

    Returns:
        dict payload describing the confirmation card.
    """
    _request_pause(tool_context)
    return {
        "_action_card": True,
        "kind": "confirm_destructive",
        "status": "confirm_destructive_pending",
        "action": action,
        "impact": impact,
        "item": item,
    }


def request_payment(
    amount: float,
    currency: str,
    reason: str,
    checkout_url: str | None = None,
    tool_context=None,
) -> dict:
    """Request a payment from the user through a payment card.

    Args:
        amount: Amount to charge, as a decimal number.
        currency: ISO-4217 currency code (e.g. ``USD``, ``EUR``).
        reason: Why the payment is requested, shown to the user.
        checkout_url: Optional hosted checkout link to redirect to.

    Returns:
        dict payload describing the payment card.
    """
    _request_pause(tool_context)
    return {
        "_action_card": True,
        "kind": "payment",
        "status": "payment_pending",
        "amount": amount,
        "currency": currency,
        "reason": reason,
        "checkout_url": checkout_url,
    }


def schedule_followup(
    when_iso: str,
    recap: str,
    calendar_link: str | None = None,
    tool_context=None,
) -> dict:
    """Propose a follow-up at a given time with a short recap.

    Args:
        when_iso: ISO-8601 datetime of the follow-up.
        recap: Short description of what will be discussed / reviewed.
        calendar_link: Optional link to the calendar event.

    Returns:
        dict payload describing the follow-up card.
    """
    _request_pause(tool_context)
    return {
        "_action_card": True,
        "kind": "followup",
        "status": "followup_pending",
        "when_iso": when_iso,
        "recap": recap,
        "calendar_link": calendar_link,
    }


def propose_artifact_edit(
    filename: str,
    diff: str,
    summary: str | None = None,
    tool_context=None,
) -> dict:
    """Propose an edit to a file as a reviewable diff card.

    Args:
        filename: Target file path.
        diff: Unified diff describing the change.
        summary: Optional human-readable summary of the edit.

    Returns:
        dict payload describing the artifact-edit card.
    """
    _request_pause(tool_context)
    return {
        "_action_card": True,
        "kind": "artifact_edit",
        "status": "artifact_edit_pending",
        "filename": filename,
        "diff": diff,
        "summary": summary,
    }


def request_file_from_user(
    purpose: str,
    accept: str | None = None,
    max_size_mb: int | None = None,
    tool_context=None,
) -> dict:
    """Ask the user to upload a file through a file-drop card.

    Args:
        purpose: Why the file is needed (shown to the user).
        accept: Optional MIME type / glob filter (e.g. ``image/*``,
            ``application/pdf``).
        max_size_mb: Optional maximum file size in megabytes.

    Returns:
        dict payload describing the file-request card.
    """
    _request_pause(tool_context)
    return {
        "_action_card": True,
        "kind": "file_request",
        "status": "file_request_pending",
        "purpose": purpose,
        "accept": accept,
        "max_size_mb": max_size_mb,
    }


def propose_agent_upgrade(
    capability: str,
    reason: str,
    skill_id: str | None = None,
    tool_name: str | None = None,
    tool_context=None,
) -> dict:
    """Propose to enable a new capability (skill or tool) on this agent.

    Use when the user's request requires a capability the agent currently
    lacks. The card lets the user approve the upgrade.

    Args:
        capability: Short description of the new capability.
        reason: Why this capability is needed for the task.
        skill_id: Optional identifier of the skill to enable.
        tool_name: Optional name of the tool to enable.

    Returns:
        dict payload describing the upgrade card.
    """
    _request_pause(tool_context)
    return {
        "_action_card": True,
        "kind": "agent_upgrade",
        "status": "agent_upgrade_pending",
        "capability": capability,
        "reason": reason,
        "skill_id": skill_id,
        "tool_name": tool_name,
    }


def embed_chart(chart_id: str, title: str | None = None) -> dict:
    """Embed an existing BI chart in the conversation.

    Args:
        chart_id: Identifier of the chart to render.
        title: Optional override title displayed above the chart.

    Returns:
        dict payload describing the chart-embed card.
    """
    # Charts are NOT auto-added to a dashboard anymore — the user decides to send
    # a chart to the BI dashboard (the card's "send" button, or by asking the
    # agent, which calls tool_send_chart_to_dashboard). So this only renders the
    # chart INLINE in the conversation.
    #
    # Verify the chart exists FIRST. Models sometimes pass an invented chart_id
    # (not the one tool_create_chart returned), which used to render a card that
    # 404s in the UI. Refuse with a clear error so the agent retries with the
    # right id. Only refuse on a definite 'missing' — a DB glitch ('unknown')
    # fails open so a real chart is never blocked.
    resolved_title = None
    try:
        from apowerb.tools_store.portfolio.business_intelligence import (
            resolve_chart_for_embed,
        )
        state, resolved_title = resolve_chart_for_embed(chart_id)
        if state == "missing":
            return {
                "success": False,
                "error": (
                    f"No chart with id '{chart_id}' exists. Use the EXACT chart_id "
                    "returned by tool_create_chart (do not invent one); if you have "
                    "not created it yet, call tool_create_chart first, then embed "
                    "the chart_id it returns."
                ),
            }
    except Exception:  # pragma: no cover - never block embedding
        resolved_title = None

    # Resolve the chart's content title so the card shows what the chart is about
    # (e.g. "Colis par département") instead of "Chart #<uuid>".
    if not title:
        title = resolved_title

    return {
        "_action_card": True,
        "kind": "chart_embed",
        "status": "chart_embed_pending",
        "chart_id": chart_id,
        "title": title,
        "dashboard_id": None,
    }


def request_location(reason: str, precision: str | None = None, tool_context=None) -> dict:
    """Ask the user to share their location via an interactive card.

    Args:
        reason: Why the location is needed (shown to the user).
        precision: Optional precision hint, typically ``coarse`` or
            ``fine``.

    Returns:
        dict payload describing the location-request card.
    """
    _request_pause(tool_context)
    return {
        "_action_card": True,
        "kind": "location_request",
        "status": "location_request_pending",
        "reason": reason,
        "precision": precision,
    }
