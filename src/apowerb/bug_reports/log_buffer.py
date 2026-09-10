"""Les lignes de log qui prouvent le bug, gardées le temps qu'on les demande.

Un utilisateur signale un défaut *après* l'avoir vu. À cet instant, la
requête fautive est terminée depuis dix secondes ou dix minutes, et ses
logs sont noyés dans le flux du conteneur. Retrouver « les cinq lignes
qui comptent » demande alors de connaître l'heure exacte, le worker, et
de savoir lire un `docker logs` — ce que la personne qui signale ne peut
pas faire, et ce que celle qui corrige fait une heure plus tard.

Ce tampon renverse la charge : chaque ligne de log est classée en
mémoire *par identifiant de requête* pendant sa fenêtre de rétention.
Le front connaît ces identifiants — le serveur les lui renvoie dans
l'en-tête ``X-Request-ID`` de chaque réponse — donc il les joint au
signalement, et le serveur ressort les lignes correspondantes.

Pourquoi pas th2pulse. L'ingest de télémétrie stocke bien les logs, mais
son API de requête filtre par ``conversation_id``, ``service``,
``level``, ``since`` et ``user_id`` — **pas** par identifiant de requête
(mesuré sur ``th2pulse/ingest/app.py`` au 10/09/2026). Interroger pulse
donnerait « tous les logs de cette minute-là », pas « les logs de CET
appel ». Les deux sont complémentaires : ce tampon fournit la preuve
exacte, pulse fournit le contexte autour.

⚠️ Limite assumée, à ne pas découvrir en production : le tampon vit dans
**un** processus. Avec plusieurs workers ou plusieurs répliques, le
signalement peut atterrir sur un worker qui n'a pas servi la requête
fautive, et le rapport ressort alors sans lignes. C'est pourquoi les
identifiants bruts sont **toujours** conservés dans le ticket, même
quand aucune ligne n'est retrouvée : ils restent cherchables dans les
logs du conteneur. Un déploiement qui veut la garantie complète branche
un ``BugReportLogSource`` externe (voir ``resolve_logs``).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

# Combien d'identifiants de requête distincts on garde, et combien de
# lignes par identifiant. Les deux plafonds existent pour la même raison :
# ce tampon est en mémoire dans le processus qui sert les requêtes, et
# une fuite ici se paie en RAM sur le chemin chaud.
DEFAULT_MAX_REQUESTS = 512
DEFAULT_MAX_LINES_PER_REQUEST = 50
DEFAULT_RETENTION_SECONDS = 30 * 60


class RequestLogBuffer:
    """Anneau borné : identifiant de requête → dernières lignes de log.

    Sûr vis-à-vis des threads : un serveur ASGI sert plusieurs requêtes
    de front, et un `logging.Handler` est appelé depuis n'importe
    laquelle d'entre elles.
    """

    def __init__(
        self,
        *,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        max_lines: int = DEFAULT_MAX_LINES_PER_REQUEST,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._entries: "OrderedDict[str, deque[dict]]" = OrderedDict()
        self._stamps: dict[str, float] = {}
        self._max_requests = max_requests
        self._max_lines = max_lines
        self._retention = retention_seconds
        self._clock = clock
        self._lock = threading.Lock()

    # -- écriture ---------------------------------------------------------

    def add(self, request_id: str, line: dict) -> None:
        if not request_id:
            return
        now = self._clock()
        with self._lock:
            bucket = self._entries.get(request_id)
            if bucket is None:
                bucket = deque(maxlen=self._max_lines)
                self._entries[request_id] = bucket
            bucket.append(line)
            self._stamps[request_id] = now
            self._entries.move_to_end(request_id)
            self._evict_locked(now)

    def _evict_locked(self, now: float) -> None:
        # Périmés d'abord : une requête ancienne part même si le tampon
        # n'est pas plein, sinon un serveur peu chargé garde des lignes
        # pendant des jours.
        expired = [
            key
            for key, stamp in self._stamps.items()
            if now - stamp > self._retention
        ]
        for key in expired:
            self._entries.pop(key, None)
            self._stamps.pop(key, None)
        # Puis le plafond, du plus ancien au plus récent.
        while len(self._entries) > self._max_requests:
            key, _ = self._entries.popitem(last=False)
            self._stamps.pop(key, None)

    # -- lecture ----------------------------------------------------------

    def get(self, request_id: str) -> list[dict]:
        with self._lock:
            bucket = self._entries.get(request_id)
            return list(bucket) if bucket else []

    def collect(self, request_ids: Iterable[str], *, limit: int = 200) -> list[dict]:
        """Lignes de plusieurs requêtes, triées dans le temps et bornées.

        Le tri est chronologique et non « par requête » : celui qui lit le
        ticket veut la séquence telle qu'elle s'est produite.
        """
        lines: list[dict] = []
        for request_id in request_ids:
            for line in self.get(request_id):
                lines.append({**line, "request_id": request_id})
        lines.sort(key=lambda item: item.get("timestamp") or "")
        if len(lines) > limit:
            # On garde la FIN : l'erreur est au bout, pas au début.
            lines = lines[-limit:]
        return lines

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._stamps.clear()

    def __len__(self) -> int:  # pragma: no cover - confort de debug
        with self._lock:
            return len(self._entries)


class BugReportLogHandler(logging.Handler):
    """Handler qui range chaque ligne dans le tampon, par requête.

    ``request_id_provider`` est injecté plutôt qu'importé pour que ce
    module reste testable sans monter une requête HTTP — et pour qu'un
    déploiement qui nomme autrement son identifiant de corrélation puisse
    brancher le sien.
    """

    def __init__(
        self,
        buffer: RequestLogBuffer,
        request_id_provider: Callable[[], Optional[str]],
        *,
        level: int = logging.WARNING,
    ) -> None:
        super().__init__(level=level)
        self._buffer = buffer
        self._request_id = request_id_provider

    def emit(self, record: logging.LogRecord) -> None:
        # Un handler qui lève tue la ligne de log qu'il devait garder, et
        # parfois la requête avec. On avale, comme le veut la convention
        # de `logging`.
        try:
            request_id = self._request_id()
            if not request_id:
                return
            self._buffer.add(request_id, self._format_record(record))
        except Exception:  # pragma: no cover - défensif par contrat
            self.handleError(record)

    def _format_record(self, record: logging.LogRecord) -> dict:
        # Import tardif : `redaction` est stdlib-pur, mais le garder hors
        # du chemin d'import du module évite un cycle si l'expurgation
        # venait un jour à journaliser quelque chose.
        from apowerb.bug_reports.redaction import redact_text

        line = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage(), max_length=2000),
        }
        if record.exc_info:
            try:
                line["exception"] = redact_text(
                    self.format(record).split("\n", 1)[-1], max_length=4000
                )
            except Exception:  # pragma: no cover
                pass
        return line


# Instance de processus. Un module-niveau plutôt qu'un attribut d'app :
# le handler est branché sur le logger racine, qui est lui aussi un objet
# de processus, et les deux doivent avoir exactement la même durée de vie.
_BUFFER = RequestLogBuffer()
_HANDLER: Optional[BugReportLogHandler] = None


def get_buffer() -> RequestLogBuffer:
    return _BUFFER


def install(level: int = logging.WARNING) -> BugReportLogHandler:
    """Branche le handler sur le logger racine. Idempotent.

    Appelé au démarrage. Deux appels ne posent pas deux handlers : le
    second remplace le premier, sinon un rechargement à chaud
    dupliquerait chaque ligne.
    """
    global _HANDLER
    from apowerb.helpers.request_id_middleware import get_request_id

    root = logging.getLogger()
    if _HANDLER is not None:
        root.removeHandler(_HANDLER)
    _HANDLER = BugReportLogHandler(_BUFFER, get_request_id, level=level)
    root.addHandler(_HANDLER)
    return _HANDLER


def uninstall() -> None:
    global _HANDLER
    if _HANDLER is not None:
        logging.getLogger().removeHandler(_HANDLER)
        _HANDLER = None


__all__ = [
    "BugReportLogHandler",
    "DEFAULT_MAX_LINES_PER_REQUEST",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_RETENTION_SECONDS",
    "RequestLogBuffer",
    "get_buffer",
    "install",
    "uninstall",
]
