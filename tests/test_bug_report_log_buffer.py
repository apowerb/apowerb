"""Les lignes de log qui prouvent le bug, retrouvées par identifiant de requête.

Sans ce tampon, un signalement arrive avec « ça a planté » et la trace
serveur est à chercher à la main dans le flux du conteneur, sans heure
exacte. Avec lui, le client rend les identifiants que le serveur lui a
donnés (`X-Request-ID`) et le serveur ressort les lignes exactes.

th2pulse ne remplace pas ce tampon : son API de requête filtre par
conversation, service, niveau et date — pas par identifiant de requête
(mesuré sur `th2pulse/ingest/app.py`, 10/09/2026). Elle donnerait « les
logs de cette minute-là », pas « les logs de CET appel ».
"""

import logging

from apowerb.bug_reports.log_buffer import BugReportLogHandler, RequestLogBuffer


def _logger_avec(buffer, request_id_box, level=logging.WARNING, name="test.buffer"):
    handler = BugReportLogHandler(buffer, lambda: request_id_box["id"], level=level)
    logger = logging.getLogger(name)
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


def test_les_lignes_sont_rangees_par_requete():
    buffer = RequestLogBuffer()
    box = {"id": "req-a"}
    logger = _logger_avec(buffer, box)

    logger.error("échec du chargement de l'agent")
    box["id"] = "req-b"
    logger.error("autre requête, autre problème")

    a = buffer.get("req-a")
    assert len(a) == 1 and "chargement de l'agent" in a[0]["message"]
    assert len(buffer.get("req-b")) == 1


def test_une_ligne_hors_requete_nest_pas_gardee():
    """Une tâche de fond ne pollue pas le tampon d'un utilisateur."""
    buffer = RequestLogBuffer()
    logger = _logger_avec(buffer, {"id": None})
    logger.error("tâche planifiée")
    assert len(buffer) == 0


def test_le_niveau_filtre_le_bruit():
    """INFO remplirait la fenêtre et en ferait sortir l'erreur cherchée."""
    buffer = RequestLogBuffer()
    logger = _logger_avec(buffer, {"id": "r"}, level=logging.WARNING)
    logger.info("chargement normal")
    logger.warning("attention")
    assert len(buffer.get("r")) == 1


def test_les_secrets_sont_expurges_avant_le_stockage():
    """Le tampon alimente une issue : rien ne doit y entrer en clair."""
    buffer = RequestLogBuffer()
    logger = _logger_avec(buffer, {"id": "r"})
    logger.error("appel refusé password=xxxxxx")
    assert "password=<redacted>" in buffer.get("r")[0]["message"]


def test_le_nombre_de_lignes_par_requete_est_borne():
    buffer = RequestLogBuffer(max_lines=3)
    logger = _logger_avec(buffer, {"id": "r"})
    for i in range(10):
        logger.error("ligne %s", i)
    lignes = buffer.get("r")
    assert len(lignes) == 3
    # On garde la FIN : l'erreur est au bout de la séquence.
    assert "ligne 9" in lignes[-1]["message"]


def test_le_nombre_de_requetes_est_borne():
    buffer = RequestLogBuffer(max_requests=2)
    box = {"id": "r0"}
    logger = _logger_avec(buffer, box)
    for i in range(5):
        box["id"] = f"r{i}"
        logger.error("x")
    assert len(buffer) == 2
    assert buffer.get("r0") == []
    assert buffer.get("r4") != []


def test_les_lignes_perimees_partent_meme_sans_pression():
    """Un serveur peu chargé ne doit pas garder des lignes des jours durant."""
    horloge = {"t": 1000.0}
    buffer = RequestLogBuffer(retention_seconds=60, clock=lambda: horloge["t"])
    box = {"id": "vieux"}
    logger = _logger_avec(buffer, box)
    logger.error("ancien")

    horloge["t"] += 120
    box["id"] = "neuf"
    logger.error("récent")

    assert buffer.get("vieux") == []
    assert buffer.get("neuf") != []


def test_collect_ordonne_dans_le_temps_et_borne():
    """Celui qui lit le ticket veut la séquence, pas un tri par requête."""
    buffer = RequestLogBuffer()
    buffer.add("r1", {"timestamp": "2026-09-10T10:00:02", "message": "second"})
    buffer.add("r2", {"timestamp": "2026-09-10T10:00:01", "message": "premier"})
    lignes = buffer.collect(["r1", "r2"])
    assert [ligne["message"] for ligne in lignes] == ["premier", "second"]
    assert lignes[0]["request_id"] == "r2"


def test_collect_garde_la_fin_quand_il_faut_couper():
    buffer = RequestLogBuffer()
    for i in range(10):
        buffer.add("r", {"timestamp": f"2026-09-10T10:00:{i:02d}", "message": f"m{i}"})
    lignes = buffer.collect(["r"], limit=3)
    assert [ligne["message"] for ligne in lignes] == ["m7", "m8", "m9"]


def test_une_exception_est_jointe_a_la_ligne():
    buffer = RequestLogBuffer()
    logger = _logger_avec(buffer, {"id": "r"})
    try:
        raise ValueError("racine du problème")
    except ValueError:
        logger.exception("échec du traitement")
    ligne = buffer.get("r")[0]
    assert "exception" in ligne
    assert "racine du problème" in ligne["exception"]
