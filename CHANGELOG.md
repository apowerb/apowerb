# Changelog

All notable changes to `apowerb` are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## What a release owes its consumers

This package is published to PyPI and to Docker Hub, and installs of it are
upgraded in place. An upgrade that changes an observable contract without a
written note is a silent breakage — one such change (see _Unreleased_ below)
turned a dead orchestrator into a healthy-looking empty dashboard and went
three weeks unnoticed. So:

- **Every backwards-incompatible change to a public contract gets an entry
  here _before_ it is released**, under `### Changed` or `### Removed`, saying
  what the old behaviour was, what the new one is, and what a consumer has to
  do about it.
- **New capabilities get an entry** under `### Added`.
- Bug fixes that a consumer might have coded around get an entry under
  `### Fixed`. Pure internals — refactors, tests, comments — do not.

### The public contract

Only these are covered by the promise above. Everything else is an
implementation detail and may change in any release:

- every HTTP route this package mounts, with its documented status codes and
  response shapes (see [docs.apowerb.com/api-reference](https://docs.apowerb.com/api-reference));
- the `apowerb` CLI;
- `apowerb.scheduler.run_agent_background.schedule_agent_run` /
  `trigger_agent_run_now`, and the `OrchestratorUnavailable` exception raised
  out of the scheduler and BI-refresh routes;
- any name a module lists in its `__all__`.

The orchestrator client classes (`MageAPIClient`, `Th2etlAPIClient`), the
helpers in `apowerb.scheduler.th2etl_client`, and anything not in the list
above are **not** public API. Import them at your own risk; they are not
guaranteed across releases and their changes are not tracked here.

## Versioning

`apowerb` follows [Semantic Versioning](https://semver.org/). While the
version is below `1.0.0`, the **minor** number carries the "breaking" signal:

| Change | Version move |
|---|---|
| Backwards-incompatible change to a public contract | minor (`0.1.x` → `0.2.0`) |
| New capability, nothing broken | minor |
| Bug fix, internal change, docs | patch (`0.2.0` → `0.2.1`) |

The published version is written from the git release tag at publish time
(`.github/workflows/pypi-publish.yml`); the `version` field in
`pyproject.toml` is only a placeholder kept roughly in step with the next
intended release. **The tag is the source of truth, and it must follow the
table above** — a release that carries a `### Changed` or `### Removed` entry
is tagged as at least a minor bump.

---

## [Unreleased] — next release tag: `v0.2.0`

The change below is a backwards-incompatible change to a public contract, so
the next release is a minor bump (`0.1.x` → `0.2.0`), not a patch.

### Changed

- **Scheduled runs now fail loud when the orchestrator is unreachable**
  ([#92](https://github.com/apowerb/apowerb/pull/92)).

  Previously, when the configured orchestrator (Mage or th2etl) could not be
  reached — connection refused, DNS failure, timeout, a gateway `502/503/504`,
  or a `200` whose body would not parse or was missing the key it promised —
  the orchestrator client degraded silently, returning `[]`, `None` or
  `False`. The scheduler routes (`/api/pipelines`, `/api/pipeline-schedules`,
  …) and the BI dashboard-refresh routes then rendered an empty, healthy
  result over a dead orchestrator.

  Now those failures raise `OrchestratorUnavailable`, and the affected routes
  answer **`503`** with a message naming the cause, instead of `200` with an
  empty body. A route that has a genuine negative answer (fetching one run
  that does not exist) still returns it as before; only "nobody answered" and
  "the answer was unreadable" become `503`.

  **If you consume the scheduler or BI-refresh HTTP routes:** treat `503`
  from them as "the orchestrator is down", not as a request error, and do not
  read a missing `503` as proof that the list is really empty.

  **If you call `MageAPIClient` or `Th2etlAPIClient` directly** (not public
  API — see above): 28 methods across the two classes now raise
  `OrchestratorUnavailable` on an unreachable orchestrator rather than
  returning a falsy value, and the annotated return type of some methods
  (including `get_pipeline_run` on both classes) widened to
  `dict[str, Any] | None`. Wrap calls in `try/except OrchestratorUnavailable`,
  and use `degrade_unless_unreachable` if you want the previous
  degrade-on-a-real-answer behaviour.

[Unreleased]: https://github.com/apowerb/apowerb/releases
