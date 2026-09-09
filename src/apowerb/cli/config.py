"""``apowerb config`` — appliquer la configuration posée depuis l'écran.

Une seule commande, appelée par l'ENTRYPOINT de l'image AVANT que le serveur
ne démarre. Voir ``core/config_admin/overlay.py`` pour la raison pour laquelle
c'est le seul emplacement honnête, et ``docker/entrypoint.sh`` pour le
câblage.
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
    historique de shell ou une capture de terminal. Ce qui s'affiche, ce sont
    les NOMS exportés — de quoi diagnostiquer un démarrage sans rien révéler.

    Usage dans l'ENTRYPOINT de l'image (``docker/entrypoint.sh``) :

        apowerb config export-env --out /tmp/overlay.env
        set -a; . /tmp/overlay.env; set +a; rm -f /tmp/overlay.env
        exec "$@"
    """
    # Import tardif : ``apowerb config --help`` ne doit pas ouvrir de base.
    from apowerb.core.config_admin.overlay import write_overlay

    try:
        names = write_overlay(out)
    except Exception as exc:  # noqa: BLE001
        # Le NOM de l'erreur, jamais son texte. Une panne de connexion porte
        # l'URL de la base dans son message, et cette commande tourne au
        # démarrage d'un conteneur, c'est-à-dire droit dans les journaux du
        # déploiement. Le diagnostic tient dans la classe de l'erreur ; ce
        # qu'elle cite ne s'y met pas.
        typer.echo(
            f"[config] configuration non appliquée ({type(exc).__name__}). "
            "Base injoignable, système de fichiers en lecture seule ou "
            "ENCRYPT_KEY absente sont les causes usuelles.",
            err=True,
        )
        # Code non nul : l'ENTRYPOINT saura qu'il ne doit pas sourcer le
        # fichier, et démarrera le service sans lui.
        raise typer.Exit(code=2) from None

    if not names:
        typer.echo(
            "[config] aucune variable à appliquer (l'environnement les impose "
            "toutes, ou rien n'a été posé).",
            err=True,
        )
        return
    typer.echo(
        f"[config] {len(names)} variable(s) appliquée(s) : " + ", ".join(names),
        err=True,
    )
