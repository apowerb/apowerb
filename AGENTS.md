# apowerb — router for coding agents

> `docs/` is git-ignored here (public docs live in apowerb-docs), so agent pages live in `agent-docs/`.

This file points, it does not explain. Read only the page for the area you touch.

| You are working on… | Read first |
|---|---|
| how the backend fits together, setup, branches, commits, tests | `CONTRIBUTING.md` |
| a domain word (agent, integration, BI item, bug report…) | `agent-docs/glossary.md` (generated from the code) |
| a feature | `agent-docs/features/<feature>.md`; the model to imitate is `agent-docs/features/bug-reports.md` |
| the way work is planned and reviewed | `agent-docs/agent-workflow.md` |
| runtime configuration | `README.md`, `src/apowerb/configs/settings.py` |

A feature without a page: write `agent-docs/features/<feature>.md` by imitating `bug-reports.md`, one feature at a time. Never rewrite the docs wholesale.

## Hard rules (enforced by script, not by this file)

`python3 scripts/harness.py` runs the rules in `harness.config.json`. It is also a pre-commit hook.
Strict rules allow zero occurrence (the core never imports a `th2agent_*` extension, no debugger, no bare `except:`, no `datetime.utcnow()`).
Ratchet rules (`os.environ` outside `configs/`, `print()` outside the CLI, router imports from lower layers, `# type: ignore`) tolerate today's debt per file and refuse any increase. When you clean a file, run `python3 scripts/harness.py --update-baseline` to lock the decrease; it never goes up.

## End of task

```bash
python3 scripts/harness.py                 # must end with "harness: OK"
python3 scripts/glossaire.py --out agent-docs/glossary.md src/apowerb/models.py src/apowerb/schema   # if a model or schema changed
DB_SCHEMA="" uv run pytest -q -m "not integration"
```

Small isolated change (a message, a one-line fix): the harness and the tests of the touched module are enough.
