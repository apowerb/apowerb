"""``apowerb config`` — appliquer la configuration posée depuis l'écran.

Une seule commande, appelée par l'entrypoint du conteneur AVANT que le
serveur ne démarre. Voir ``core/config_admin/overlay.py`` pour la raison
pour laquelle c'est le seul emplacement honnête.
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(help="Configuration posée depuis l'écran d'administration")


@app.command("export-env")
def export_env(
    out: Path = typer.Option(
        ...,
        "--out",
        "-o",
        help="Fichier à écrire (créé en 0600). Obligatoire.",
    ),
) -> None:
    """Écrit les variables posées que l'environnement n'impose pas déjà.

    ``--out`` est obligatoire, et il n'y a pas de sortie sur stdout : une
    commande qui imprime des secrets finit tôt ou tard dans un journal, un
    historique de shell ou une capture de terminal. Le 04/09/26, un filtre
    par préfixe sur l'environnement d'un service a déversé un mot de passe
    ERP en clair de cette façon exactement.

    Ce qui s'affiche, ce sont les NOMS exportés — de quoi diagnostiquer un
    démarrage sans rien révéler.

    Usage dans un entrypoint :

        apowerb config export-env --out /tmp/overlay.env
        set -a; . /tmp/overlay.env; set +a; rm -f /tmp/overlay.env
        exec apowerb serve --host 0.0.0.0 --port 8000 --no-reload
    """
    # Import tardif : ``apowerb config --help`` ne doit pas ouvrir de base.
    from apowerb.core.config_admin.overlay import write_overlay

    names = write_overlay(out)
    if not names:
        typer.echo("Aucune variable à appliquer (l'environnement les impose "
                   "toutes, ou rien n'a été posé).")
        return
    typer.echo(f"{len(names)} variable(s) exportée(s) vers {out} : "
               + ", ".join(names))
