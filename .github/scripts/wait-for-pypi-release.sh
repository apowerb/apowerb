#!/usr/bin/env bash
# Waits until installing a specific PyPI package version resolves, instead
# of guessing a fixed sleep.
#
# Why this exists: the Docker publish workflow runs on workflow_run of the
# PyPI publish workflow completing, then the Dockerfile immediately runs
# `uv pip install --system --no-cache-dir "apowerb==$VERSION"`. PyPI's
# index takes some time to propagate a freshly published release, so
# building right away is a race. Releases 0.1.11 and 0.1.12 both failed the
# Docker build with:
#
#   Because there is no version of apowerb==X and you require
#   apowerb==X, we can conclude that your requirements are unsatisfiable.
#
# and both succeeded on manual re-run once PyPI had caught up.
#
# This script runs the exact same class of command the Dockerfile runs
# (`uv pip install --dry-run --system`), so it fails and succeeds under the
# same condition as the real build - it is not a proxy for it. An earlier
# version polled PyPI's JSON metadata endpoint (/pypi/<name>/<version>/json)
# instead; that endpoint is a different code path from the Simple index the
# resolver actually reads, and can report "available" before the resolver
# agrees - which would pass this step while the real Docker build still
# fails. `--dry-run` performs full dependency resolution without installing
# or downloading anything.
#
# Required env vars:
#   PACKAGE_NAME     - PyPI project name, e.g. apowerb
#   PACKAGE_VERSION  - exact release version to wait for, e.g. 0.1.12
# Optional env vars:
#   MAX_ATTEMPTS     - default 90
#   SLEEP_SECONDS    - default 10
#   EXTRA_UV_ARGS    - extra space-separated arguments forwarded to
#                      `uv pip install` (tests use this to point uv at a
#                      local package source instead of the real PyPI index)
set -euo pipefail

: "${PACKAGE_NAME:?PACKAGE_NAME is required}"
: "${PACKAGE_VERSION:?PACKAGE_VERSION is required}"
# 90 x 10s = 15 minutes. The first budget was 5 minutes and it was not enough:
# the 0.1.13 release exhausted all 30 attempts on 2026-08-05 and failed the
# publish, while 0.1.11 and 0.1.12 had resolved comfortably inside it earlier
# the same day. Propagation time is not a constant, so the budget is sized for
# the slow case rather than the median.
#
# A wide budget costs nothing when propagation is quick -- the loop exits on
# the first successful resolution, it does not wait out its allowance.
MAX_ATTEMPTS="${MAX_ATTEMPTS:-90}"
SLEEP_SECONDS="${SLEEP_SECONDS:-10}"

IFS=' ' read -r -a extra_uv_args <<< "${EXTRA_UV_ARGS:-}"

requirement="${PACKAGE_NAME}==${PACKAGE_VERSION}"

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  # --break-system-packages only matters if the target interpreter is
  # PEP 668 externally-managed; harmless here since --dry-run never writes
  # anything regardless.
  if uv pip install --system --break-system-packages --dry-run "${extra_uv_args[@]}" "$requirement"; then
    echo "${requirement} resolves (attempt ${attempt}/${MAX_ATTEMPTS})."
    exit 0
  fi

  echo "${requirement} does not resolve yet (attempt ${attempt}/${MAX_ATTEMPTS})."

  if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
    sleep "$SLEEP_SECONDS"
  fi
done

echo "::error::${requirement} never resolved after $((MAX_ATTEMPTS * SLEEP_SECONDS))s."
exit 1
