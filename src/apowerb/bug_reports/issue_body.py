"""Rendu d'un signalement en corps d'issue lisible.

L'ordre des sections est celui dans lequel on corrige un bug, pas celui
dans lequel les informations sont arrivées : ce qu'on observe, comment le
reproduire, puis la preuve. Quelqu'un qui ouvre le ticket doit savoir en
dix lignes s'il sait le corriger — le contexte technique vient après, et
il est replié.

Le corps est du Markdown pur : GitHub le rend, un webhook générique le
transporte tel quel, et un humain le lit sans rendu.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

_SEVERITY_LABEL = {
    "blocker": "🛑 Bloquant — l'utilisateur ne peut plus travailler",
    "major": "⚠️ Majeur — fonctionnalité cassée, contournement pénible",
    "minor": "▪️ Mineur — gêne, contournement simple",
    "cosmetic": "🎨 Cosmétique — affichage",
}

_MAX_LOG_LINES = 40
_MAX_API_CALLS = 15
_MAX_CONSOLE = 15


def _plain(value: Any) -> Any:
    """La valeur d'un membre d'énumération, ou la valeur telle quelle.

    `issue_payload` construit son dictionnaire avec `model_dump()`, qui rend
    les membres d'énumération sans les convertir. Une classe `(str, Enum)`
    garde le `__str__` d'`Enum` : `str(BugArea.CHAT)` vaut « BugArea.CHAT »,
    pas « chat ». Le 11/09/2026, l'en-tête de `apowerb/roadmap#26` affichait
    ainsi « BugArea.CHAT » et « BugSeverity.BLOCKER » à des lecteurs humains.

    Normaliser ici plutôt que chez l'appelant : ce module reçoit un
    dictionnaire de provenances variées — base, API, test — et c'est le
    dernier endroit avant le texte publié.
    """
    return getattr(value, "value", value)


def _area_label(area: Any) -> str | None:
    """Libellé lisible de la zone, jamais son identifiant seul.

    L'identifiant reste affiché entre parenthèses : c'est lui qu'on tape
    dans un filtre de recherche d'issues.
    """
    if not area:
        return None
    from apowerb.bug_reports.areas import AREA_LABELS, BugArea

    try:
        known = BugArea(str(area))
    except ValueError:
        return str(area)
    return f"{AREA_LABELS[known]} (`{known.value}`)"


def _table(rows: Iterable[tuple[str, Any]]) -> str:
    kept = [(label, value) for label, value in rows if value not in (None, "", [], {})]
    if not kept:
        return ""
    lines = ["| | |", "|---|---|"]
    lines += [f"| **{label}** | {value} |" for label, value in kept]
    return "\n".join(lines)


def _fenced(lines: Iterable[str], language: str = "") -> str:
    body = "\n".join(lines)
    return f"```{language}\n{body}\n```" if body else ""


def _format_log_line(line: Mapping[str, Any]) -> str:
    stamp = str(line.get("timestamp") or "")[:23]
    level = (line.get("level") or "").ljust(7)
    logger = line.get("logger") or ""
    message = line.get("message") or ""
    rendered = f"{stamp} {level} {logger} — {message}"
    exception = line.get("exception")
    if exception:
        rendered += "\n" + str(exception)
    return rendered


def _format_api_call(call: Mapping[str, Any]) -> str:
    method = (call.get("method") or "?").upper()
    status = call.get("status")
    duration = call.get("duration_ms")
    bits = [f"{method} {call.get('path') or '?'}", f"→ {status if status is not None else '(pas de réponse)'}"]
    if duration is not None:
        bits.append(f"{float(duration):.0f} ms")
    if call.get("request_id"):
        bits.append(f"request_id={call['request_id']}")
    if call.get("error"):
        bits.append(f"erreur: {call['error']}")
    return "  ".join(bits)



def _format_trail(trail: Iterable[Mapping[str, Any]]) -> str:
    """Le chemin parcouru, du plus ancien au plus récent.

    Rendu en une ligne par écran plutôt qu'en flèches enchaînées : les
    routes sont longues, et le temps passé sur chacune est une donnée —
    trois secondes sur un écran veut dire « traversé », trois minutes
    veut dire « c'est là que ça s'est joué ».
    """
    lines = []
    for step in trail:
        label = step.get("label") or step.get("route") or "?"
        route = step.get("route")
        dwell = step.get("dwell_ms")
        bit = f"{label}"
        if route and route != label:
            bit += f"  ({route})"
        if dwell is not None:
            seconds = float(dwell) / 1000
            bit += f"  — {seconds:.0f} s" if seconds >= 1 else "  — <1 s"
        lines.append(bit)
    return "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))


def render_issue_title(report: Mapping[str, Any]) -> str:
    """Titre court, préfixé par la route : c'est elle qui situe le défaut."""
    title = (report.get("title") or "").strip()
    route = (report.get("route") or "").strip()
    if not title:
        observed = (report.get("observed") or report.get("what_i_did") or "").strip()
        title = observed.splitlines()[0][:120] if observed else "Signalement sans titre"
    if route and route not in title:
        return f"[{route}] {title}"[:200]
    return title[:200]


def render_issue_body(report: Mapping[str, Any], *, app_url: str | None = None) -> str:
    """Corps Markdown complet.

    ``report`` est le dictionnaire déjà expurgé produit par le service —
    cette fonction ne masque rien, elle met en forme. Le faire ici aurait
    signifié qu'un appelant qui l'oublie publie des secrets ; c'est en
    amont, à l'écriture en base, que l'expurgation a lieu, une fois.
    """
    severity = str(_plain(report.get("severity")) or "major")
    occurrences = int(report.get("occurrences") or 1)
    context: Mapping[str, Any] = report.get("context") or {}

    sections: list[str] = []

    header = _table(
        [
            ("Fonctionnalité", _area_label(_plain(report.get("area")))),
            ("Sévérité", _SEVERITY_LABEL.get(severity, severity)),
            (
                "Signalements",
                f"{occurrences} (même empreinte `{report.get('fingerprint')}`)"
                if occurrences > 1
                else None,
            ),
            ("Route", f"`{report.get('route')}`" if report.get("route") else None),
            ("Version interface", context.get("app_version")),
            ("Version serveur", report.get("server_version")),
            ("Run serveur", f"`{report.get('run_id')}`" if report.get("run_id") else None),
            ("Signalé par", report.get("reporter_email")),
            ("Survenu le", context.get("occurred_at")),
        ]
    )
    if header:
        sections.append(header)

    observed = report.get("observed")
    expected = report.get("expected")
    if observed or expected:
        block = []
        if observed:
            block.append(f"**Observé** — {observed}")
        if expected:
            block.append(f"**Attendu** — {expected}")
        sections.append("\n\n".join(block))

    where_bits: list[str] = []
    if report.get("where_i_was"):
        where_bits.append(str(report["where_i_was"]))
    machine_place = context.get("screen") or context.get("route")
    if machine_place:
        section_name = context.get("section")
        located = f"`{machine_place}`" + (f" — {section_name}" if section_name else "")
        where_bits.append(f"Écran détecté : {located}")
    last_action = context.get("last_action") or {}
    if last_action.get("label"):
        kind = last_action.get("kind") or "action"
        where_bits.append(f"Dernier geste : **{last_action['label']}** ({kind})")
    trail = list(context.get("navigation_trail") or [])
    if trail:
        where_bits.append(
            "Chemin parcouru avant le défaut :\n\n" + _format_trail(trail)
        )
    if where_bits:
        sections.append("## Où j'étais\n\n" + "\n\n".join(where_bits))

    if report.get("what_i_did"):
        sections.append(f"## Reproduction\n\n{report['what_i_did']}")

    server_logs = list(report.get("server_logs") or [])
    if server_logs:
        shown = server_logs[-_MAX_LOG_LINES:]
        omitted = len(server_logs) - len(shown)
        note = (
            f"\n_{omitted} ligne(s) plus ancienne(s) omise(s)._\n" if omitted > 0 else ""
        )
        sections.append(
            "## Logs serveur de la requête fautive\n\n"
            "Lignes capturées côté serveur pour les identifiants de requête "
            "joints par le client — c'est la trace de l'appel lui-même, pas "
            "un extrait de la même minute.\n\n"
            + _fenced([_format_log_line(line) for line in shown], "text")
            + note
        )
    elif report.get("request_ids"):
        sections.append(
            "## Logs serveur\n\n"
            "⚠️ Aucune ligne retrouvée en mémoire pour ce signalement — la "
            "fenêtre de rétention était dépassée, ou la requête a été servie "
            "par un autre worker. Les identifiants restent cherchables dans "
            "les logs bruts :\n\n"
            + _fenced([str(rid) for rid in report["request_ids"]], "text")
        )

    api_calls = list(report.get("api_calls") or [])
    if api_calls:
        shown = api_calls[-_MAX_API_CALLS:]
        sections.append(
            "<details><summary>Derniers appels d'API vus par le client "
            f"({len(shown)})</summary>\n\n"
            + _fenced([_format_api_call(call) for call in shown], "text")
            + "\n</details>"
        )

    console = list(report.get("console") or [])
    if console:
        shown = console[-_MAX_CONSOLE:]
        rendered = [
            f"[{(entry.get('level') or '?').upper()}] {entry.get('message') or ''}"
            + (f"  ({entry['source']})" if entry.get("source") else "")
            for entry in shown
        ]
        sections.append(
            f"<details><summary>Console du navigateur ({len(shown)})</summary>\n\n"
            + _fenced(rendered, "text")
            + "\n</details>"
        )

    environment = _table(
        [
            ("Navigateur", context.get("user_agent")),
            ("Fenêtre", context.get("viewport")),
            ("Langue", context.get("locale")),
            ("Thème", context.get("theme")),
            ("Agent", context.get("agent_id")),
            ("Conversation", context.get("conversation_id")),
            ("URL", context.get("url")),
        ]
    )
    if environment:
        sections.append(
            "<details><summary>Environnement</summary>\n\n" + environment + "\n</details>"
        )

    if report.get("has_screenshot"):
        if app_url:
            link = f"{app_url.rstrip('/')}/admin/bug-reports/{report.get('id')}"
            sections.append(
                f"🖼️ **Capture d'écran jointe** — visible dans l'écran de triage : {link}\n\n"
                "_Elle n'est pas publiée ici : elle montre l'écran d'un "
                "utilisateur, donc potentiellement des données qui ne nous "
                "appartiennent pas._"
            )
        else:
            sections.append(
                "🖼️ **Capture d'écran jointe** — consultable dans l'écran de "
                "triage du déploiement."
            )

    sections.append(
        f"---\n<sub>Signalement #{report.get('id')} · empreinte "
        f"`{report.get('fingerprint')}` · créé depuis l'application</sub>"
    )

    return "\n\n".join(section for section in sections if section)


__all__ = ["render_issue_body", "render_issue_title"]
