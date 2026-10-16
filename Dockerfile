# Prototype : Wolfi (Chainguard) au lieu de python:3.13-slim (Debian trixie).
# Les quatre HIGH restants sur la base Debian -- libtinfo6, libacl1,
# libsystemd0, libudev1 -- n'ont aucun correctif publie pour trixie ; Wolfi
# reconstruit ses paquets des que l'amont corrige. Python est fixe par le nom
# du paquet apk (python-3.13), pas par l'etiquette de l'image, et les deux
# etages utilisent le meme : les liens du venv pointent vers /usr/bin/python3.13
# des deux cotes. glibc, comme Debian : les roues manylinux (pyodbc,
# psycopg2-binary, asyncpg, duckdb...) s'installent telles quelles.
FROM cgr.dev/chainguard/wolfi-base AS builder

ARG APPOWERB_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Build-time only: compiler toolchain and dev headers, for any dependency
# without a wheel. None of this ships in the runtime image below.
RUN apk add --no-cache python-3.13 build-base unixodbc-dev

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

COPY pyproject.toml uv.lock ./

RUN uv venv --python /usr/bin/python3.13 /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv sync --no-dev --no-install-project \
    # `[otel]` pulls th2pulse, which is what bridges application log records to
    # the collector. Without it apowerb.configs.observability finds no th2pulse,
    # logs a warning naming the missing extra, and the Logging screen stays empty
    # however the deployment is configured -- measured on the published 0.2.9
    # image, 2026-09-07. The extra is declared in pyproject.toml but was never
    # installed here.
    #
    # `uv` is required, not incidental: apowerb's own dependency set has a
    # pre-existing fsspec conflict between `pins` and `s3fs` that makes plain
    # `pip install apowerb` unresolvable, with or without this extra. uv resolves
    # it. Do not swap this line for pip.
    && if [ -z "${APPOWERB_VERSION}" ]; then \
        VIRTUAL_ENV=/opt/venv uv pip install --no-cache-dir "apowerb[otel]"; \
    else \
        VIRTUAL_ENV=/opt/venv uv pip install --no-cache-dir "apowerb[otel]==${APPOWERB_VERSION}"; \
    fi

FROM cgr.dev/chainguard/wolfi-base AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Runtime-only packages:
# - python-3.13: the interpreter the venv was built against (no pip: Wolfi
#   ships it separately as py3.13-pip, and nothing here needs it).
# - unixodbc: shared library required by pyodbc (MSSQL BI connector).
# - tzdata: zoneinfo, as on the Debian base.
#
# Ce que la base Debian purgeait a la main (perl-base, util-linux et ses
# binaires, libuuid1, ncurses, pip) n'est pas dans wolfi-base : rien a retirer.
# Le shell est celui de busybox (sh), suffisant pour docker/entrypoint.sh
# (`.`, `set -a`, `exec`, `rm`) ; il n'y a ni bash ni coreutils GNU.
#
# Consequence pour qui etend cette image (FROM apowerb/apowerb) : le
# gestionnaire de paquets est `apk add`, plus `apt-get install`. Les paquets
# Debian (ex. msodbcsql18 du depot Microsoft) ne s'installent plus tels quels.
RUN apk add --no-cache python-3.13 unixodbc tzdata

COPY --from=builder /opt/venv /opt/venv

# Applique la configuration posee depuis l'ecran d'administration avant de
# lancer la commande de l'image. Ici et pas dans le chart Helm : le chart ne
# couvrirait que Kubernetes, en laissant derriere Compose, l'hebergement
# manage et les VM. L'image est le seul point commun aux quatre.
#
# Le script ne peut pas empecher le demarrage : base injoignable, systeme de
# fichiers en lecture seule ou sous-commande absente d'une version publiee plus
# ancienne se traversent toutes, et le service demarre alors avec son
# environnement seul -- exactement son comportement d'avant.
COPY docker/entrypoint.sh /usr/local/bin/apowerb-entrypoint
RUN chmod +x /usr/local/bin/apowerb-entrypoint

EXPOSE 8000

# ENTRYPOINT en forme exec, CMD inchange : le chart et les deux composes le
# passent tel quel, et `docker run <image> <autre commande>` continue de
# remplacer le CMD sans court-circuiter l'application de la configuration.
ENTRYPOINT ["/usr/local/bin/apowerb-entrypoint"]
CMD ["apowerb", "serve", "--host", "0.0.0.0", "--port", "8000", "--no-reload"]
