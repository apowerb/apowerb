#!/bin/sh
#
# Applique la configuration posée depuis l'écran d'administration, puis passe
# la main à la commande de l'image.
#
# Pourquoi ici, et pas dans le chart Helm : le faire dans le chart ne couvrirait
# que Kubernetes et laisserait derrière Compose, l'hébergement managé et les
# déploiements sur VM — dont ceux qu'on ne connaît pas. L'image est le seul
# point commun aux quatre. Vérifié le 09/09/2026 : ni le chart
# (`backend-deployment.yaml`), ni `docker-compose.yml`, ni
# `docker-compose/docker-compose.yml` ne posent `command:` ou `entrypoint:` sur
# le service du cœur, donc l'ENTRYPOINT de l'image s'applique partout.
#
# Les valeurs sont chiffrées en base. `export-env` n'écrit QUE les variables
# que l'environnement ne porte pas déjà — la précédence, c'est l'environnement
# qui gagne — dans un fichier créé en 0600, que l'on source puis efface. Les
# valeurs ne vivent ensuite que dans l'environnement de ce processus, comme
# n'importe quelle variable du déploiement.
#
# ─────────────────────────────────────────────────────────────────────────────
# Ce script ne doit JAMAIS empêcher le service de démarrer.
#
# Pas de `set -e`, et c'est délibéré. Trois pannes sont attendues et toutes
# trois se traversent :
#
#  1. la base n'est pas joignable au démarrage (elle démarre en parallèle, le
#     réseau n'est pas prêt, l'installation n'en a pas encore) ;
#  2. le système de fichiers est en lecture seule et le fichier ne peut pas
#     être écrit ;
#  3. `apowerb-config-env` n'existe pas — l'image installe `apowerb` depuis
#     PyPI, donc une image bâtie sur une version publiée antérieure à cette
#     fonctionnalité n'a pas la commande. Le shell rend 127, que le `if`
#     ci-dessous attrape comme n'importe quel autre échec. Sans cette
#     tolérance, ajouter cet ENTRYPOINT casserait au démarrage toutes les
#     images épinglées sur une version antérieure — exercé en CI le
#     09/09/2026 sur l'image bâtie depuis `apowerb` 0.2.12 publié, où les
#     quatre conteneurs du smoke test ont démarré malgré l'échec.
#
# Dans les trois cas le service démarre avec son environnement seul, ce qui est
# exactement son comportement d'avant. L'écran, lui, dit « en attente de
# redémarrage » : le défaut est visible, il n'est pas silencieux.
# ─────────────────────────────────────────────────────────────────────────────

OVERLAY="${APOWERB_CONFIG_OVERLAY_FILE:-/tmp/apowerb-config-overlay.env}"

# `apowerb-config-env`, et non `apowerb config export-env` qui fait pourtant la
# même chose : la sous-commande passe par `apowerb.cli.main`, qui importe
# uvicorn et les trois autres sous-applications — dont `cli.runs`, qui
# construit Settings. 4,0 s mesurées contre 0,41 s pour le script dédié, et ces
# 4 s se paieraient à chaque démarrage de conteneur, chaque redémarrage de pod
# et chaque mise à jour progressive, avant même que le serveur ne commence.
#
# Sortie sur stderr : stdout de ce conteneur appartient au serveur. La commande
# n'imprime jamais une valeur, seulement les NOMS qu'elle a exportés.
if apowerb-config-env --out "$OVERLAY" >&2; then
    # `set -a` exporte tout ce que le fichier définit ; il est refermé juste
    # après pour ne pas exporter par inadvertance ce que la commande finale
    # définirait.
    set -a
    # shellcheck disable=SC1090
    . "$OVERLAY"
    set +a
else
    echo "[entrypoint] configuration posée depuis l'écran : NON appliquée (cause ci-dessus)." >&2
    echo "[entrypoint] le service démarre avec son environnement seul." >&2
fi

# Effacé dans tous les cas, y compris quand la lecture a échoué à mi-course.
rm -f "$OVERLAY"

# `exec` : le serveur devient PID 1 et reçoit SIGTERM directement, sans quoi
# l'arrêt d'un pod attendrait le délai de grâce complet à chaque déploiement.
# `"$@"` est le CMD de l'image, inchangé — et reste remplaçable par les
# arguments d'un `docker run`, comme avant cet ENTRYPOINT.
exec "$@"
