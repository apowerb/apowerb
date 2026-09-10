"""Le second signalement d'un défaut déjà connu ne doit pas échouer.

Mode d'échec vécu, trouvé le 10/09/2026 par un parcours réel contre un
PostgreSQL et invisible pour les autres tests, qui n'ont pas de base :

    le PREMIER signalement passait — il n'y a pas de canonique à relire ;
    le SECOND rendait 500 avec `MissingGreenlet`.

Cause : `AsyncSession.commit()` expire les objets de la session. Lire
ensuite `canonical.occurrences` déclenche un rechargement paresseux, donc
une entrée-sortie, et en asyncio SQLAlchemy refuse d'en faire hors du
contexte greenlet. Le correctif lit les deux valeurs AVANT le commit.

Le test reproduit exactement cette sémantique sans base : la session
simulée expire son objet au commit, et tout accès ultérieur lève. Un test
qui se contenterait de compter les appels ne dirait rien du défaut.
"""

import pytest

from apowerb.schema.bug_report_schema import BugReportCreate


class ExpiredAttributeError(RuntimeError):
    """Tient le rôle de `MissingGreenlet` : accès après expiration."""


class SignalementCanonique:
    """Objet de session qui refuse d'être lu après un commit."""

    def __init__(self, ident: int, occurrences: int):
        self._id = ident
        self._occurrences = occurrences
        self.expire = False

    def _garde(self):
        if self.expire:
            raise ExpiredAttributeError(
                "attribut expiré : le relire déclencherait une E/S paresseuse"
            )

    @property
    def id(self):
        self._garde()
        return self._id

    @property
    def occurrences(self):
        self._garde()
        return self._occurrences

    @occurrences.setter
    def occurrences(self, valeur):
        self._garde()
        self._occurrences = valeur


class ResultatUnique:
    def __init__(self, valeur):
        self._valeur = valeur

    def scalar_one_or_none(self):
        return self._valeur


class SessionSimulee:
    """Reproduit la seule sémantique qui compte : commit expire tout."""

    def __init__(self, canonique):
        self._canonique = canonique
        self.ajoutes = []

    async def execute(self, *_args, **_kwargs):
        return ResultatUnique(self._canonique)

    def add(self, objet):
        self.ajoutes.append(objet)

    async def commit(self):
        if self._canonique is not None:
            self._canonique.expire = True

    async def refresh(self, objet):
        # La ligne fraîchement écrite, elle, est rechargée par le commit.
        objet.id = 42


@pytest.mark.asyncio
async def test_un_doublon_ne_relit_pas_le_canonique_apres_le_commit():
    from apowerb.bug_reports import service

    canonique = SignalementCanonique(ident=7, occurrences=3)
    db = SessionSimulee(canonique)

    charge = BugReportCreate(
        what_i_did="Cliquer sur Exécuter",
        observed="Rien",
        context={"route": "/agents/42/tools"},
    )

    # Sans le correctif, cet appel lève ExpiredAttributeError — comme il
    # levait MissingGreenlet en conditions réelles.
    resultat = await service.create_bug_report(
        db, user_id=1, reporter_email="t@example.org", payload=charge
    )

    assert resultat.duplicate_of == 7, "le doublon doit pointer vers le canonique"
    assert resultat.occurrences == 4, "le compteur du canonique doit être incrémenté"


@pytest.mark.asyncio
async def test_un_signalement_neuf_reste_a_une_occurrence():
    """Le chemin sans canonique — celui qui passait déjà — ne régresse pas."""
    from apowerb.bug_reports import service

    db = SessionSimulee(canonique=None)
    charge = BugReportCreate(what_i_did="x", context={"route": "/agents"})

    resultat = await service.create_bug_report(
        db, user_id=1, reporter_email="t@example.org", payload=charge
    )
    assert resultat.duplicate_of is None
    assert resultat.occurrences == 1
