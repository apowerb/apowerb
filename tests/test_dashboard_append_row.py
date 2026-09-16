"""Ou se pose un composant que l agent ajoute a un dashboard deja rempli.

L agent choisissait lui-meme sa ligne et sa colonne, avec 0/0 par defaut, et
l instruction du modele lui donnait des gabarits fixes ("Row 0 : les KPI,
Row 2 : les graphiques"). Sur un dashboard vierge cela tombe juste ; sur un
dashboard deja construit, le nouvel element atterrit au milieu de ceux que
l utilisateur avait ranges.

Le contrat vise : une position non precisee veut dire "sous tout le reste".
"""

import inspect

import pytest

from apowerb.bi.dashboards.core import (
    Dashboard,
    DashboardComponent,
    GridPosition,
)


def _chart(row: int, height: int, col: int = 0, width: int = 6) -> DashboardComponent:
    return DashboardComponent.from_chart(
        "chart-x",
        position=GridPosition(row=row, col=col, width=width, height=height),
    )


class TestNextFreeRow:
    def test_un_dashboard_vide_commence_a_zero(self):
        assert Dashboard.create(title="vide").next_free_row() == 0

    def test_se_pose_sous_le_composant_le_plus_bas(self):
        db = Dashboard.create(title="d", components=[_chart(row=0, height=4)])
        assert db.next_free_row() == 4

    def test_tient_compte_de_la_hauteur_pas_seulement_de_la_ligne(self):
        # Le piege : prendre max(row) donnerait 2, et le nouveau composant
        # recouvrirait celui qui occupe les lignes 2 a 8.
        db = Dashboard.create(
            title="d",
            components=[_chart(row=0, height=2), _chart(row=2, height=6)],
        )
        assert db.next_free_row() == 8

    def test_ignore_l_ordre_de_la_liste(self):
        db = Dashboard.create(
            title="d",
            components=[_chart(row=10, height=2), _chart(row=0, height=4)],
        )
        assert db.next_free_row() == 12

    def test_des_composants_cote_a_cote_ne_s_empilent_pas(self):
        # Deux KPI sur la meme ligne : la ligne libre suivante est sous eux,
        # pas sous leur somme.
        db = Dashboard.create(
            title="d",
            components=[
                _chart(row=0, height=2, col=0, width=6),
                _chart(row=0, height=2, col=6, width=6),
            ],
        )
        assert db.next_free_row() == 2


class TestContratDesOutils:
    """Une position non precisee doit vouloir dire "empile", pas "ligne 0"."""

    @pytest.mark.parametrize("nom", ["tool_add_chart_to_dashboard", "tool_add_kpi_to_dashboard"])
    def test_la_ligne_par_defaut_est_la_sentinelle(self, nom):
        from apowerb.tools_store.portfolio import business_intelligence as bi

        sig = inspect.signature(getattr(bi, nom))
        assert sig.parameters["row"].default == -1, (
            "row=0 par defaut fait atterrir chaque ajout en haut du dashboard"
        )
