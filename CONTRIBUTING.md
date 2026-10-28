# Contributing to apowerb

Thanks for taking the time. This file describes how this repository actually
works — the conventions below were read off the history and the tooling, not
invented for the occasion.

## Ways to contribute

You do not need to write code to be useful here.

- **Report a bug.** A clear reproduction is worth more than a fix. Say what you
  ran, what you expected, and what happened instead.
- **Improve the docs.** If something tripped you up, it will trip up the next
  person. The documentation lives in
  [apowerb-docs](https://github.com/apowerb/apowerb-docs).
- **Pick up a scoped issue.** Issues labelled
  [`good first issue`](https://github.com/apowerb/apowerb/labels/good%20first%20issue)
  are small and carry their context, file pointers and a "done when" list.
  [`help wanted`](https://github.com/apowerb/apowerb/labels/help%20wanted) marks
  the ones we most want taken. Comment on an issue before you start, so two
  people do not build the same thing.
- **Tell us what you built.** Deployed apowerb or built an agent on it? Post it in
  [Discussions](https://github.com/apowerb/apowerb/discussions) under *Show and tell*.

## Licence and CLA

Contributions are licensed under the [Apache License 2.0](./LICENSE), like the
rest of the project. You keep the copyright on what you write.

A **Contributor License Agreement** is required before a pull request can be
merged. Two things it does, and one it does not:

- It records that you actually hold the rights to what you contribute. The
  usual reason someone does not is code written while employed elsewhere —
  which is worth settling before the code ships, not after.
- It lets the project be relicensed later without tracking down every past
  contributor. This repository moved from MIT to Apache-2.0 in a single day
  precisely because every line belonged to thaink²; that would not have been
  true with a dozen external contributors and no agreement.
- It does **not** unlock commercial use of your contribution. Apache-2.0
  already permits that, provided attribution and the `NOTICE` file are kept.
  The CLA relieves us of those obligations — it does not create the right.

The text is in [CLA.md](./CLA.md). A bot comments on your first pull request and asks you
to sign by replying to it; one signature covers everything you contribute afterwards, and
signatures are recorded in `signatures/cla.json` in this repository.

A lighter alternative exists, should the CLA prove heavy for what it buys: the **DCO**
(Developer Certificate of Origin), a `Signed-off-by:` line in each commit. It covers the
first point above and neither of the other two.

Note also that the licence covers the code and not the marks: see
[TRADEMARK.md](./TRADEMARK.md) before naming a fork or reusing the logo.

## How apowerb fits together

Two things surprise people, so here they are first:

- **Agents are data, not files.** An agent is a row in the database: its model,
  instruction, tools and sub-agents. The core writes a small module per agent
  (`root_agent = to_agent(...)`) so the ADK runtime can find it, but that file
  holds no logic: `to_agent` resolves everything from the database at runtime
  (`src/apowerb/core/adk_agent_builder.py`, `src/apowerb/core/agent_helpers/`).
  To change how agents behave, you are usually in `core/`, not in a generated file.
- **Models go through LiteLLM.** You rarely touch a provider SDK: a model string
  (`DEFAULT_LLM_MODEL`, or the one set on an agent) is wrapped in ADK's `LiteLlm`,
  so OpenAI, Anthropic, Mistral, Gemini or a local model behave the same.

This repository is the backend: the FastAPI app, the agent runtime, tools,
skills and the `apowerb` CLI, all under `src/apowerb/`. The rest of the stack
lives in sibling repositories, and
[apowerb-hosting](https://github.com/apowerb/apowerb-hosting) wires it together
with Docker Compose and a Helm chart:

| Component | Repository | In the default stack |
|---|---|---|
| Backend and agent runtime | this one | yes |
| Web UI (Next.js) | [apowerb-ui](https://github.com/apowerb/apowerb-ui) | yes |
| ETL and automation pipelines | [th2etl](https://github.com/apowerb/th2etl) | optional (`etl` profile) |
| OpenTelemetry collection and observability | [th2pulse](https://github.com/apowerb/th2pulse) | optional (`pulse` / `logs` profiles) |
| Time-series forecasting service | [th2forecast](https://github.com/apowerb/th2forecast) | optional (`forecast` profile) |

## Getting set up

The package targets Python 3.10+ (`requires-python = ">=3.10"`); CI runs on
3.13. Dependencies are managed with [uv](https://docs.astral.sh/uv/), and the
lockfile is committed — do not hand-edit `uv.lock`.

```bash
uv sync --group dev
uv run pre-commit install
```

Copy `.env.example` to `.env` before running the server. `README.md` covers
configuration and the runtime; this file only covers contributing.

## Branches and pull requests

Work on a branch named `<type>/<short-slug>`, matching the commit type:
`feat/setup-status`, `fix/sync-engines-pre-ping`, `docs/name-the-runtime-root-variable`,
`chore/remove-pr-body-draft`.

Open the pull request against `main`. Keep one subject per PR — a fix and an
unrelated refactor are two PRs. In the description, say what you changed, and
say **what you verified and how**: the command you ran and what it printed. A PR
that claims a behaviour without showing the check is the main reason a review
stalls here.

Read `main` shortly before you open the PR. This repository moves fast, and a
branch cut a few days ago can already be answering a question someone else has
closed.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/), with an optional
scope. The types in use are `feat`, `fix`, `chore`, `docs`, `test`, `refactor`
and `perf`; append `!` for a breaking change (`feat!:`).

Subjects are written in English, lowercase, no trailing period. The house style
is to describe **the behaviour that now holds**, not the edit that was made —
someone reading `git log` should learn what the software does differently:

```
fix(agents): an omitted field means unchanged, not cleared
fix(db): the long-lived synchronous engines ping before they hand out a connection
fix(ci): build the Docker image from the commit that produced the wheel
feat(bi): uploads land in a local directory by default, S3 only when fully configured
```

Prefer that to `fix(agents): update patch handler`.

## Style and static checks

`pre-commit` is the gate, and it is the same set of hooks a reviewer will expect
to have passed:

- **ruff** — lint (`--fix`) and format. There is no `[tool.ruff]` section in
  `pyproject.toml`, so ruff's defaults apply; do not add a local override in a
  feature PR.
- **mypy** — static typing.
- basic hygiene — `check-yaml`, `check-toml`, `end-of-file-fixer`,
  `trailing-whitespace`, `check-added-large-files`.
- `uv sync`, so the lockfile stays in step with `pyproject.toml`.

```bash
uv run pre-commit run --all-files
```

## Tests

Tests live in `tests/`. `asyncio_mode` is `auto`, so async tests need no
decorator. Two markers are declared: `unit` and `integration` — integration
tests expect a live server on `localhost:8000` and are deselected everywhere
else.

The unit suite, as CI runs it:

```bash
DB_SCHEMA="" uv run pytest -q -m "not integration"
```

`DB_SCHEMA=""` matters: the models qualify their tables with `settings.db_schema`,
and SQLite has no schemas, so an unqualified name is what lets the suite run
without a Postgres.

Test the guarantee, not the shape of the failure. Asserting that an exception
type is raised says little; assert the state, the returned value, or the side
effect the contract promises. When you fix a bug, add the regression test first
and watch it fail — a test written after the fix has never proven anything.

### The ignore list is declared debt

`.github/workflows/tests.yml` excludes a short list of test files from the blocking
run, then runs them again in a non-blocking step so they stay visible. That list
is meant to **shrink**. Removing a file from it — by fixing the tests — is a
welcome contribution on its own. Adding a file to it needs a reason in the PR
description, and is close to never the right answer.

## Security

Do not report vulnerabilities through issues or pull requests. See
[SECURITY.md](./SECURITY.md) for the private disclosure path.

## Getting unstuck

- **Questions, ideas, "is this a bug?":** open a thread in
  [Discussions](https://github.com/apowerb/apowerb/discussions).
- **A concrete problem with the code:** open an issue.
- Unsure about the test setup or an approach? Open a draft pull request and say
  where you are stuck. A PR with a gap is easier to help with than no PR.
