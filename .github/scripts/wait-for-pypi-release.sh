#!/usr/bin/env bash
# Polls PyPI's per-version JSON API until a specific package release is
# installable, instead of guessing a fixed sleep.
#
# Why this exists: the Docker publish workflow runs on workflow_run of the
# PyPI publish workflow completing, then immediately does
# `uv pip install "apowerb==$VERSION"`. PyPI's index takes some time to
# propagate a freshly published release, so building right away is a race.
# Releases 0.1.11 and 0.1.12 both failed the Docker build with "Because
# there is no version of apowerb==X and you require apowerb==X, we can
# conclude that your requirements are unsatisfiable", and both succeeded on
# manual re-run once PyPI had caught up.
#
# Required env vars:
#   PACKAGE_NAME     - PyPI project name, e.g. apowerb
#   PACKAGE_VERSION  - exact release version to wait for, e.g. 0.1.12
# Optional env vars:
#   MAX_ATTEMPTS     - default 30
#   SLEEP_SECONDS    - default 10
#   PYPI_BASE_URL    - default https://pypi.org/pypi (overridable for tests)
set -euo pipefail

: "${PACKAGE_NAME:?PACKAGE_NAME is required}"
: "${PACKAGE_VERSION:?PACKAGE_VERSION is required}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-30}"
SLEEP_SECONDS="${SLEEP_SECONDS:-10}"
PYPI_BASE_URL="${PYPI_BASE_URL:-https://pypi.org/pypi}"

url="${PYPI_BASE_URL}/${PACKAGE_NAME}/${PACKAGE_VERSION}/json"

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  status="$(curl -s -o /dev/null -w '%{http_code}' "$url")"

  if [ "$status" = "200" ]; then
    echo "${PACKAGE_NAME}==${PACKAGE_VERSION} is available on PyPI (attempt ${attempt}/${MAX_ATTEMPTS})."
    exit 0
  fi

  echo "${PACKAGE_NAME}==${PACKAGE_VERSION} not yet available on PyPI (HTTP ${status}, attempt ${attempt}/${MAX_ATTEMPTS})."

  if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
    sleep "$SLEEP_SECONDS"
  fi
done

echo "::error::${PACKAGE_NAME}==${PACKAGE_VERSION} never became available on PyPI after $((MAX_ATTEMPTS * SLEEP_SECONDS))s."
exit 1
