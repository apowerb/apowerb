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
        VIRTUAL_ENV=/opt/venv /root/.local/bin/uv pip install --no-cache-dir "apowerb[otel]"; \
    else \
        VIRTUAL_ENV=/opt/venv /root/.local/bin/uv pip install --no-cache-dir "apowerb[otel]==${APPOWERB_VERSION}"; \
    fi

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
#
# util-linux and the command-line packages built from the same source (mount,
# login, bsdutils) carry four HIGH advisories -- CVE-2026-76642, -78408, -78409,
# -78410 -- about failed mount helpers running privileged post-hooks,
# `nsenter --join-cgroup`, and X-mount path resolution. A pure Python ASGI
# service invokes none of that: nothing in this repository, in the entrypoint,
# or in apowerb/apowerb-hosting execs mount, login, su or nsenter. Purging the
# four is a leaf operation -- apt removes nothing else -- and it takes 16 of the
# 46 OS advisories on this image with it.
#
# The shared libraries from that same source (libmount1, libblkid1, libuuid1,
# libsmartcols1, liblastlog2-2) stay: the base image links them. Scanners
# attribute a source CVE to every binary package built from it, so those five
# keep being reported even though the vulnerable code -- the mount helpers --
# is gone with the binaries above.
RUN apt-get update \
    && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends unixodbc \
    && apt-get purge -y --allow-remove-essential perl-base \
    && apt-get purge -y --allow-remove-essential util-linux mount login bsdutils \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# libuuid1 and ncurses (libncursesw6, ncurses-bin, ncurses-base) carry seven of
# the eleven HIGH advisories left on this image -- util-linux CVE-2026-76642,
# -78408, -78409, -78410 and ncurses CVE-2025-69720 -- with no fix published
# for Debian trixie (Trivy 0.68.1, 2026-10-16). Nothing in the runtime needs
# them: libuuid1 lost its last dependant with util-linux above, and only
# Python's own `_uuid` and `_curses` extension modules link these libraries.
# Both are removed with them, so `import uuid` takes its pure-Python path
# (uuid4 reads os.urandom, uuid1 falls back to a random node) and
# `import curses` fails with ImportError instead of a dangling .so.
#
# dpkg --purge, not `apt-get purge --allow-remove-essential`: dpkg refuses to
# remove a package another installed package depends on, so if a later change
# pulls in a consumer the build fails here instead of apt silently cascading.
# Only ncurses-bin and ncurses-base are Essential and need the force flag.
#
# Kept: libtinfo6 (bash, readline), libacl1 (coreutils, tar, passwd),
# libsystemd0 (apt, PAM) -- the three HIGH that remain; libudev1 goes below.
#
# Consequence for anyone extending this image: `docker exec -it ... bash`
# still works, but without terminfo `clear`/`tput` are gone and full-screen
# programs fall back to a dumb terminal. `apt-get install ncurses-base` brings
# it back.
RUN dpkg --purge libuuid1 libncursesw6 \
    && dpkg --purge --force-remove-essential ncurses-bin ncurses-base \
    && rm -f /usr/local/lib/python3.13/lib-dynload/_uuid.* \
             /usr/local/lib/python3.13/lib-dynload/_curses*

# libudev1 (systemd CVE-2026-16742, no trixie fix) lost its last consumer with
# util-linux above: apt depends on libsystemd0, not libudev1, so apt stays
# installed and `apt-get install` keeps working. dpkg refuses the purge if a
# later change brings a consumer back.
RUN dpkg --purge libudev1

# pip is never used at runtime: the application runs out of /opt/venv, which uv
# built and which carries no pip of its own. The copy shipped in the base image
# vendors its whole dependency tree under pip/_vendor, and that vendored tree --
# not the application's dependencies -- is what the setuptools (CVE-2025-47273)
# and msgpack (GHSA-6v7p-g79w-8964) advisories on this image are reported
# against. Removing pip removes both, and they are the only two Python
# advisories here that have a fix at all.
#
# Consequence for anyone extending this image (FROM apowerb/apowerb): `pip` is
# gone. Use uv, or run `python -m ensurepip` first.
RUN python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip*

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
