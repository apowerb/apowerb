# Échelle de retraits sur la base actuelle (python:3.13-slim, Debian trixie).
# RUNG=0 : purges de main (perl, util-linux, libuuid1, ncurses) -> référence.
# RUNG=1 : + dpkg --purge libudev1 seul (apt gardé) ; consigne le refus de dpkg.
# RUNG=2 : + retrait d'apt, libapt-pkg, libsystemd0, libudev1 (dpkg reste).
# RUNG=3 : + bash, libreadline, libtinfo6 (sh = dash) et le module readline.
FROM python:3.13-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/
COPY docker/eval/deps.sh /deps.sh
RUN sh /deps.sh /usr/local/bin/python3.13

FROM python:3.13-slim
ARG RUNG=0
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends unixodbc \
    && apt-get purge -y --allow-remove-essential perl-base util-linux mount login bsdutils \
    && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* \
    && dpkg --purge libuuid1 libncursesw6 \
    && dpkg --purge --force-remove-essential ncurses-bin ncurses-base \
    && rm -f /usr/local/lib/python3.13/lib-dynload/_uuid.* /usr/local/lib/python3.13/lib-dynload/_curses*
RUN if [ "$RUNG" = 1 ]; then \
      dpkg --purge libudev1 > /RUNG1.log 2>&1 || echo "REFUS" >> /RUNG1.log; cat /RUNG1.log; \
    fi
RUN if [ "$RUNG" -ge 2 ]; then \
      dpkg --purge --force-remove-essential apt $(dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' 'libapt-pkg*' | awk '/^ii/{print $2}') libsystemd0 libudev1; \
    fi
RUN if [ "$RUNG" -ge 3 ]; then \
      rm -f /usr/local/lib/python3.13/lib-dynload/readline.* \
      && dpkg --purge --force-remove-essential bash $(dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' 'libreadline*' | awk '/^ii/{print $2}') libtinfo6; \
    fi
COPY --from=builder /opt/venv /opt/venv
COPY docker/entrypoint.sh /usr/local/bin/apowerb-entrypoint
