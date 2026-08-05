FROM python:3.13-slim AS builder

ARG APPOWERB_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1

WORKDIR /app

# Build-time only: compiler toolchain and dev headers needed to install
# dependencies. None of this ships in the runtime image below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        unixodbc-dev \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && /root/.local/bin/uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv /root/.local/bin/uv sync --no-dev --no-install-project \
    && VIRTUAL_ENV=/opt/venv /root/.local/bin/uv pip install --no-cache-dir "apowerb==${APPOWERB_VERSION}"

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Runtime-only package:
# - unixodbc: shared library required by pyodbc (MSSQL BI connector) at
#   import/connect time. No -dev headers needed, nothing is compiled here.
#
# curl is intentionally NOT installed here: checked apowerb/apowerb-hosting
# (docker-compose, k8s manifests, Helm chart) and nothing execs curl inside
# this container. docker-compose has no healthcheck on the apowerb service
# at all; the Helm/k8s readiness and liveness probes use httpGet, which the
# kubelet calls over the network, not a container exec. If that changes,
# add curl back explicitly.
#
# perl-base ships as an Essential package in the upstream python:3.13-slim
# (Debian trixie) base image itself -- it is not pulled in by anything
# above. It is not used by this application at runtime (pure Python/ASGI
# service), so it is purged here. This removes the perl 5.40.1-6 CVE
# surface (CVE-2026-13221, CVE-2026-12087, both CVSS 9.1, no fix published
# for this Debian release) from the final image entirely.
#
# Consequence for anyone extending this image (FROM apowerb/apowerb):
# `apt-get install` still works afterwards for ordinary packages (dpkg/apt
# themselves don't require perl), but installing a package whose postinst
# or maintainer scripts are written in Perl (e.g. build-essential, which
# pulls in dpkg-dev -> libdpkg-perl) will re-pull perl-base automatically
# to satisfy that dependency -- apt resolves it like any other missing
# dependency, it does not error out. There is no permanently broken state;
# worst case is a slightly bigger derived image, not a failed build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends unixodbc \
    && apt-get purge -y --allow-remove-essential perl-base \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

EXPOSE 8000

CMD ["apowerb", "serve", "--host", "0.0.0.0", "--port", "8000", "--no-reload"]
