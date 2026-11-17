"""Appliquer la configuration posée depuis l'écran d'administration.

Une seule commande, appelée par l'ENTRYPOINT de l'image AVANT que le serveur
ne démarre. Voir ``core/config_admin/overlay.py`` pour la raison pour laquelle
c'est le seul emplacement honnête, et ``docker/entrypoint.sh`` pour le câblage.

Deux chemins vers la même fonction, et ce n'est pas un doublon :

- ``apowerb-config-env`` — le script console que l'ENTRYPOINT appelle. Il
  n'importe que ce module, soit 0,41 s mesurées ;
- ``apowerb config export-env`` — la sous-commande, pour qui utilise déjà la
  CLI à la main. Elle passe par ``apowerb.cli.main``, qui importe uvicorn et
  les trois autres sous-applications : 4,0 s mesurées.

Ces 4 s se paieraient sinon à chaque démarrage de conteneur, chaque
redémarrage de pod et chaque mise à jour progressive.
"""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(help="Configuration set from the admin screen")


@app.command("export-env")
def export_env(
    out: Path = typer.Option(
        ...,
        "--out",
        "-o",
        help="Output file to write (created with 0600 permissions). Required.",
    ),
) -> None:
    """Write the configured variables that the environment does not already set.

    ``--out`` is required and nothing is written to stdout: a command that
    prints secrets ends up sooner or later in a log, a shell history or a
    terminal capture. What is printed is the NAMES that were exported, enough
    to diagnose a startup without revealing anything.

    Usage in the image ENTRYPOINT (``docker/entrypoint.sh``):

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


if __name__ == "__main__":  # pragma: no cover - `python -m apowerb.cli.config`
    app()
