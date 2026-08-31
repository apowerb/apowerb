"""
core/extensions/loader.py
-------------------------
Charge les extensions au démarrage pour qu'elles enregistrent leurs briques.

Deux variables, dans cet ordre :

- ``TH2_EXTENSIONS`` : liste de modules séparés par des virgules. C'est le
  mécanisme de branchement des briques — le pack commercial, un connecteur, un
  overlay client. Chaque module expose ``register(registry)`` ou, historiquement,
  ``init_overlay(registry)``.
- ``TH2_OVERLAY_MODULE`` : l'ancienne variable, un seul module. Toujours honorée,
  chargée après les précédentes.

Aucune variable => aucune extension, le noyau reste générique et complet en
lui-même. C'est cette propriété qui permet de publier le noyau seul : le code
commercial n'est pas désactivé par un drapeau, il est **absent**, et il se
rebranche en nommant son module ici.

Toute erreur remonte (fail-fast) : une extension cassée doit être vue par le
healthcheck, jamais dégrader silencieusement le service.
"""

from __future__ import annotations

import importlib
import logging
import os

from apowerb.core.extensions.registry import registry

logger = logging.getLogger(__name__)

#: Les deux noms acceptés pour le point d'entrée d'une extension. ``register``
#: est le nom courant ; ``init_overlay`` est conservé parce qu'un overlay client
#: l'utilise en production et qu'un renommage le casserait au déploiement.
_ENTRYPOINTS = ("register", "init_overlay")


def _load_one(mod_name: str) -> str:
    mod = importlib.import_module(mod_name)
    for attr in _ENTRYPOINTS:
        fn = getattr(mod, attr, None)
        if callable(fn):
            fn(registry)
            logger.info("[EXT] chargee: %s (via %s)", mod_name, attr)
            return mod_name
    raise RuntimeError(
        f"extension {mod_name!r} n'expose aucun point d'entree appelable "
        f"parmi {_ENTRYPOINTS}"
    )


def load_extensions() -> list[str]:
    """Charge toutes les extensions declarees. Retourne les modules charges."""
    noms: list[str] = []

    for brut in (os.getenv("TH2_EXTENSIONS") or "").split(","):
        mod_name = brut.strip()
        if mod_name:
            noms.append(_load_one(mod_name))

    overlay = (os.getenv("TH2_OVERLAY_MODULE") or "").strip()
    if overlay and overlay not in noms:
        noms.append(_load_one(overlay))

    return noms


def load_overlay() -> str | None:
    """Compat : l'ancien appel, qui ne connaissait qu'un seul overlay.

    Conserve parce que ``main.py`` et les tests l'appellent, et parce que le
    deploiement client depend de son contrat de retour (le nom du module, ou None).
    """
    noms = load_extensions()
    return noms[-1] if noms else None
