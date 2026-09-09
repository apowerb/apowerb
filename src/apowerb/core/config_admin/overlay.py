"""Le moment où une valeur posée devient effective : le démarrage du processus.

Pourquoi pas à chaud, mesuré plutôt que supposé :

    grep '^settings = get_settings()' src/ → 33 modules

capturent l'objet ``Settings`` au niveau module, à l'import — dont
``integrations/microsoft.py``, ``integrations/google.py``, ``storage/s3.py``
et ``scheduler/mage.py``, c'est-à-dire précisément ceux qui lisent les
variables que cet écran configure. ``get_settings.cache_clear()`` ne les
atteint pas : leur ``settings`` est une référence figée, prise avant même
que ``bootstrap()`` ne s'exécute. Vider le cache produirait donc un
processus *partiellement* rechargé — la checklist annoncerait « configuré »
pendant que l'intégration Google continuerait d'utiliser l'ancienne valeur.
C'est plus dangereux que de ne rien recharger, et cela recoupe la suite de
tests cassée le 08/09 par une mutation à chaud de la configuration.

Et pourquoi pas à l'import de ``settings.py`` : ce serait toucher la base
au moment d'un ``import apowerb``, ce que ce dépôt s'interdit explicitement
— ``bootstrap()`` existe pour cette raison — et cela casserait tout test
qui importe le paquet sans base.

Reste donc le seul emplacement honnête : AVANT que le processus n'importe
l'application. D'où une commande, appelée par l'entrypoint du conteneur :

    apowerb config export-env --out /tmp/overlay.env
    set -a; . /tmp/overlay.env; set +a; rm -f /tmp/overlay.env
    exec apowerb serve ...

Le redémarrage du pod est le geste d'application. Explicite, atomique, et
déjà compris de l'opérateur qui l'exécute.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from sqlalchemy import create_engine

from apowerb.configs.settings import get_settings
from apowerb.core.config_admin.store import (
    OVERLAY_MARKER,
    env_holds,
    read_all_decrypted,
)
from apowerb.helpers.database_connection import DBConfig


def _quote(value: str) -> str:
    """Guillemets simples POSIX : tout est littéral sauf ``'`` lui-même.

    Sûr parce que le catalogue refuse déjà sauts de ligne et caractères de
    contrôle : les deux moitiés de la même décision (cf. ``catalog.py``).
    """
    return "'" + value.replace("'", "'\\''") + "'"


def overlay_lines(stored: dict[str, str], env=None) -> list[str]:
    """Les lignes à exporter : les variables posées que l'environnement
    n'impose PAS déjà, plus le marqueur qui dit lesquelles.

    La règle de précédence vit à un seul endroit — ``store.env_holds``, la
    même que celle dont l'écran se sert pour dire d'où vient une variable. Un
    opérateur qui pose la variable dans son déploiement reprend la main sans
    toucher à la base : sa valeur est là, l'overlay se tait.

    Le marqueur ne transporte que des NOMS. Il est ce qui permet au processus
    de reconnaître sa propre empreinte au redémarrage ; sans lui, une valeur
    posée depuis l'écran deviendrait « imposée par le déploiement » dès le
    premier redémarrage, et l'écran retirerait son champ.

    Rien à exporter, rien du tout : écrire un marqueur vide par-dessus un
    marqueur hérité effacerait l'information au lieu de la préciser.

    Pure et paramétrée pour être éprouvable sans base ni fichier.
    """
    applique = {
        name: value
        for name, value in sorted(stored.items())
        if not env_holds(name, env)
    }
    if not applique:
        return []
    lignes = [f"{name}={_quote(value)}" for name, value in applique.items()]
    lignes.append(f"{OVERLAY_MARKER}={_quote(','.join(applique))}")
    return lignes


def write_overlay(out: str | Path) -> list[str]:
    """Écrit le fichier et rend les NOMS exportés.

    Rend des noms, jamais des valeurs : l'appelant en fait un message pour
    l'opérateur, et le 04/09 a montré ce que coûte une sortie qui contient
    « juste » l'environnement d'un service — un filtre par préfixe avait
    déversé un mot de passe ERP en clair sur un terminal.

    Le fichier est créé en 0600 AVANT d'être écrit : le créer puis le
    protéger laisse une fenêtre, si courte soit-elle, pendant laquelle son
    contenu est lisible par tout le monde sur cette machine.
    """
    settings = get_settings()
    sync_url = DBConfig().get_db_url().replace("postgresql+asyncpg://", "postgresql://")
    engine = create_engine(sync_url, echo=False)
    with engine.connect() as conn:
        stored = read_all_decrypted(settings.db_schema, conn)

    lines = overlay_lines(stored)
    # Le marqueur est de la plomberie, pas une configuration : il part dans le
    # fichier, jamais dans le compte rendu fait à l'opérateur.
    noms = [
        line.split("=", 1)[0]
        for line in lines
        if not line.startswith(f"{OVERLAY_MARKER}=")
    ]

    path = Path(out)
    # os.open plutôt que Path.write_text : le mode passe à la création.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                 stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + ("\n" if lines else ""))
    except BaseException:
        # Un fichier partiel serait sourcé tel quel par l'entrypoint, donc
        # une configuration à moitié appliquée sans que rien ne le signale.
        path.unlink(missing_ok=True)
        raise
    return noms
