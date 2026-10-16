# Wolfi (Chainguard) : base gratuite wolfi-base (busybox sh + apk), Python
# fixé par le nom du paquet apk, pas par l'étiquette de l'image.
FROM cgr.dev/chainguard/wolfi-base AS builder
RUN apk add --no-cache python-3.13
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/
COPY docker/eval/deps.sh /deps.sh
RUN sh /deps.sh /usr/bin/python3.13

FROM cgr.dev/chainguard/wolfi-base
RUN apk add --no-cache python-3.13 unixodbc
COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.sh /usr/local/bin/apowerb-entrypoint
