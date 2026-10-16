# Distroless Python Debian 13. Son Python est celui de Debian
# (/usr/bin/python3.13) : le venv doit être construit sur la même
# installation, sinon ses liens pointent vers un /usr/local absent.
ARG TAG=latest
FROM debian:13-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/
COPY docker/eval/deps.sh /deps.sh
RUN sh /deps.sh /usr/bin/python3.13

FROM gcr.io/distroless/python3-debian13:${TAG}
COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.sh /usr/local/bin/apowerb-entrypoint
