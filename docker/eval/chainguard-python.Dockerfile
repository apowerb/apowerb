# Image Chainguard python : seule l'étiquette latest est gratuite, la version
# de Python suit donc celle que Chainguard publie ce jour-là.
FROM cgr.dev/chainguard/python:latest-dev AS builder
USER root
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/
COPY docker/eval/deps.sh /deps.sh
RUN sh /deps.sh "$(readlink -f /usr/bin/python3)"

FROM cgr.dev/chainguard/python:latest
COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.sh /usr/local/bin/apowerb-entrypoint
ENTRYPOINT []
