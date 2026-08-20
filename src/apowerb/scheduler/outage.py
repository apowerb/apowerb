"""One answer to an orchestrator that cannot be reached, shared by every route.

The scheduler router and the dashboard-refresh router call the same
orchestrator client, so they meet the same outage. Keeping the translation
here rather than inside one of them is what stops the two from drifting: how
loudly to report a missing orchestrator is a judgement about the deployment,
not about whichever endpoint happened to notice.
"""

from __future__ import annotations

from fastapi import HTTPException

# Everything that belongs to each orchestrator, not just its URL.
#
# Reading the URL alone had it backwards. An operator who installs Mage on the
# documented default port has no reason to touch `BASE_URL` -- it already
# matches the deployment -- but `scheduler/mage.py` refuses to build a client
# without `API_KEY`, so that is the field they do set. They would have been
# classified as having configured nothing, and a real outage of their Mage
# logged at DEBUG under a message saying no orchestrator was configured.
#
# So the question is not "did they name the address" but "did they engage with
# this orchestrator at all". Any one of these says yes.
ORCHESTRATOR_SETTINGS = {
    "mage": (
        "base_url",
        "api_key",
        "oauth_token",
        "project_name",
        "mage_pipeline_uuid",
    ),
    "th2etl": ("th2etl_base_url", "th2etl_api_key"),
}

NOT_CONFIGURED = (
    "No orchestrator is configured in this installation. Scheduled runs need "
    "one (Mage or th2etl); set its URL, or set SCHEDULER_ENABLED=false to drop "
    "the feature and its screen entirely."
)


def orchestrator_is_configured(settings) -> bool:
    """Did this installation ever name an orchestrator?

    `model_fields_set` holds what the environment actually provided -- the
    only way to tell "left at the shipped default" from "typed in" once the
    settings object is built. Any single setting of the selected orchestrator
    counts: the question is whether somebody engaged with it, not whether they
    happened to name its address.

    ⚠️ On its own this does not mean "no orchestrator here": one genuinely
    running beside the API on `localhost:6789` needs no configuration at all,
    and an install like that is perfectly sound. What carries the meaning is
    the **conjunction** at the call site -- nothing configured *and* the call
    just failed. Either half alone says nothing, which is also why this must
    not become a startup warning: at boot the second half is unknown.

    The asymmetry is deliberate. A deployment that sets `API_KEY` for some
    unrelated reason is read as having a Mage, and a genuine absence then
    logs at ERROR -- noise. The opposite mistake silences a real outage, which
    is the failure this whole thing exists to prevent. When in doubt, be loud.

    Reads the settings of the orchestrator actually **selected**: a deployment
    switched to th2etl that only ever configured Mage engaged with nothing the
    client will use.
    """
    names = ORCHESTRATOR_SETTINGS.get(
        str(settings.orchestrator).strip().lower(), ORCHESTRATOR_SETTINGS["mage"]
    )
    return bool(set(names) & settings.model_fields_set)


def outage_response(exc, doing: str, settings, logger) -> HTTPException:
    """The one answer every route gives to an unreachable orchestrator.

    503 and never 500, never an empty result: the screen must be able to say
    "the orchestrator is down" instead of "you have no pipelines" -- and a 500
    reads as a broken server, when what happened is that an optional component
    is not answering. Returning an empty list is worse still: a dead
    orchestrator then renders as a perfectly healthy, empty dashboard, which is
    how one outage went three weeks unnoticed (see ``OrchestratorUnavailable``).

    Two situations wear the same exception and must not wear the same log
    level. An orchestrator nobody installed is not one that is down: a
    deployment carrying no Mage logged ERROR on every visit to the screen,
    quoting a localhost address its operator never typed -- and an ERROR
    meaning "you did not install an optional component" teaches whoever reads
    the logs to scroll past ERROR lines. So DEBUG plus a sentence saying how to
    turn the feature off, against ERROR plus the real cause.

    Takes `settings` and `logger` as arguments rather than reading its own:
    each router keeps its own logger, so the line lands under the module that
    actually served the request, and tests that patch a router's `get_settings`
    keep reaching this decision.

    Per route rather than one handler on the app: these routers are mounted by
    consumers on their own FastAPI app (see ``_OrchestratorClientProxy``), and
    a handler registered in ``main.py`` would leave every one of them with the
    500s this replaces.
    """
    if not orchestrator_is_configured(settings):
        logger.debug("no orchestrator configured while %s; %s", doing, exc)
        return HTTPException(status_code=503, detail=NOT_CONFIGURED)
    logger.error("orchestrator unreachable while %s: %s", doing, exc)
    return HTTPException(status_code=503, detail=str(exc))
