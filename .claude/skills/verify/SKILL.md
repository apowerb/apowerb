---
name: verify
description: Start the apowerb backend (FastAPI) locally on a throwaway Postgres and exercise its real flows (health, sign-up and login, agent, LLM-free workflow) to prove a change works, before any commit, push or PR. Use after every code change in this repository, and whenever asked to "verify", "test it for real" or "check that it works".
---

# Verify apowerb (backend) for real

This skill is the repository's smoke-test harness. Keep it fast and reliable: if it is broken or
slow, fix it before relying on it. A code change never edits this file in the same commit to make
itself pass: updating the skill is a separate commit.

The repository's own commands (`AGENTS.md`, `Justfile`) are reused as-is in step 1.

## Target

- Repository: the root of this clone, or a worktree. Never switch the branch of a clone that
  another task is using: `git worktree add ../<task> origin/main --detach`.
- Environment: **local only**. Database = throwaway Postgres container (`postgres:16-alpine`,
  127.0.0.1:56432, test password), unrelated to production, staging or anyone else's database.
  The server creates its own tables on startup (`bootstrap()` in `main.py`): no migration to run,
  and never one against a database that isn't yours.
- Secrets: only `ENCRYPT_KEY`, generated at random into a 0600 file (never printed, never read
  back). No LLM key: flows that call a model are reported "not verified" (the repository has no
  mock model mode).
- Prerequisites: `uv` on the PATH, Python 3.13 managed by uv, Docker usable by the current user,
  ports 56432 (database) and 18000 (API) free. `uv sync` reuses the uv cache (a few seconds): do not
  clear it to save space.

## 0. Test environment (once per run)

```bash
export PATH=$HOME/.local/bin:$PATH
cd <worktree-or-clone>                       # repository root
V=$HOME/tmp/verify-apowerb-run; mkdir -p $V
ss -ltn | grep -E ':(56432|18000) ' && echo "PORT IN USE: pick another" # must print nothing
python3 -c "import base64,os;print('ENCRYPT_KEY='+base64.urlsafe_b64encode(os.urandom(32)).decode())" > $V/env
chmod 600 $V/env
uv sync --group dev --frozen
```

## 1. Cheap checks first (commands from `AGENTS.md` / `Justfile`)

```bash
python3 scripts/harness.py                                  # must end with "harness: OK"
set -a; . $V/env; set +a                                    # tests need a valid ENCRYPT_KEY (as in CI)
uv run --frozen pytest -q -m "not integration" -p no:cacheprovider
```

`just test` runs `DB_SCHEMA="" uv run pytest -q -m "not integration"`: same suite. Measured
duration: about 4 minutes (7000 passed, 9 skipped, 33 deselected). Without `ENCRYPT_KEY` in the
environment, dozens of tests fail for an environment reason, not a code one. For a small change,
`AGENTS.md` accepts the harness plus the tests of the touched module. A failure here stops
everything: no point starting the application.
If `just lint` is required: `uv run pre-commit run --all-files` (slow, off the fast path).

## 2. Start the database and the API

```bash
docker run -d --name verify-apowerb-pg -e POSTGRES_PASSWORD=verify -e POSTGRES_DB=apowerb_verify \
  -p 127.0.0.1:56432:5432 postgres:16-alpine
for i in $(seq 1 30); do docker exec verify-apowerb-pg pg_isready -U postgres -q && break; sleep 1; done

set -a; . $V/env; set +a
export DB_HOST=127.0.0.1 DB_PORT=56432 DB_NAME=apowerb_verify DB_USER=postgres DB_PASSWORD=verify \
       DB_SSLMODE=disable WORKING_MODE=development RUNTIME_ROOT=$V/rt
nohup uv run --frozen apowerb serve --host 127.0.0.1 --port 18000 --no-reload > $V/server.log 2>&1 &
echo $! > $V/server.pid; date +%s > $V/server.start
for i in $(seq 1 60); do [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18000/health/live)" = 200 ] && break; sleep 1; done
curl -s http://127.0.0.1:18000/health/ready    # {"status":"ready","checks":{"database":{"ok":true,...},"fernet":{"ok":true,...}}}
```

- Ready when `/health/live` returns 200 (about 7 s measured), then `/health/ready` confirms the
  database and the key. Wait with a bounded poll, not a fixed `sleep`.
- `DB_SSLMODE=disable` is required: the default is `require` and the throwaway Postgres has no
  SSL ("server does not support SSL" at startup).
- `RUNTIME_ROOT` moves `agents_pool/`, `artifacts_store/` and `uploads/` out of the repository.
- `$V/server.pid` is the PID of `uv run`; the Python server is its child (see step 5).
- Record the start time (`server.start`) so only this process gets stopped.

## 3. Flows to exercise (HTTP requests, `curl`)

Base: `B=http://127.0.0.1:18000/api`. API routes live under `/api`, except `/health*`.
Pick the flows the diff touches; when in doubt, all of them.

| Flow | Steps | Expected result |
|---|---|---|
| Health | `GET /health/live`, `/health/ready`, `/health` | 200; `ready` with `database.ok` and `fernet.ok` true; `/health` → `{"status":"ok"}` |
| Sign-up | `POST $B/users/` JSON `{"email":"verify@apowerb-verify.com","password":"Verify-Pass-123","first_name":"V","last_name":"B"}` | 200, `user_id` 1, `role` USER (a `.test`/`.example` domain is rejected with 422) |
| Login | `POST $B/auth/token` as a form `username=<email>&password=<pwd>` | 200, `{"token_type":"bearer","access_token":"…"}`; wrong password → 401 |
| Auth required | `GET $B/users/me` and `GET $B/agents` with `Authorization: Bearer <token>`, then without | 200 with the token; 401 `Not authenticated` without |
| Agent (CRUD) | `POST $B/agents` `{"agent_name":"verify_agent","agent_model":"gemini/gemini-2.5-flash","agent_description":"d","agent_instruction":"ok","agent_type":"llm"}`; `GET $B/agents/agent1`; `DELETE $B/agents/agent1` | 200 `Agent registered successfully` (id `agent1` on a fresh database), read back with `agent_id`, 200 `Agent deleted successfully`. Writes `$V/rt/agents_pool/agent1` |
| LLM-free workflow | `POST $B/workflows/defs/validate`, then `POST $B/workflows/defs`, then `POST $B/workflows/defs/<workflow_id>/run` (SSE) with the graph below | `{"valid":true,"errors":[]}`; creation: JSON with `workflow_id`, `status` draft, `version` 1; stream `run_started`, `node_complete` for t, s, c, o, `route` "true", then `done` with `"ok bonjour Ada"`; `GET $B/workflows/runs` lists the run as `success` |

Workflow graph (no model calls; request bodies: `{"name":…,"graph":…}` to create,
`{"graph":…}` to validate, `{"payload":{"nom":"Ada"}}` to run):

```json
{"version":1,"nodes":[
 {"id":"t","type":"trigger","config":{"kind":"manual"}},
 {"id":"s","type":"set","config":{"fields":[{"key":"msg","value":"bonjour {{t.nom}}"}]}},
 {"id":"c","type":"condition","config":{"rules":[{"field":"{{s.msg}}","op":"contains","value":"bonjour"}]}},
 {"id":"o","type":"output","config":{"value":"ok {{s.msg}}"}}],
 "edges":[{"source":"t","target":"s"},{"source":"s","target":"c"},{"source":"c","target":"o","route":"true"}]}
```

Run the workflow with `curl -sN -m 60` (SSE stream). Put JSON bodies in files (`-d @file`) rather
than inline: nested quotes break easily, especially over `ssh`.

Not verified here, on purpose: running an agent (`/api/adk/...`, a workflow `agent` node), RAG,
connected tools (Google, Microsoft…), OAuth, e-mails: they all need a paid model or an external
account. Report them as "not verified". Dev auth bypass: `BYPASS_AUTH=true` creates a fake ADMIN
(`test@example.com`) without a user database; refused when `WORKING_MODE=production`; not
exercised here (the real login above works).

## 4. Evidence

- Keep request outputs and `$V/server.log` (never `$V/env` nor a full token) in the session's
  scratch space, and state in the report what was seen, flow by flow.
- A flow that was not exercised is "not verified", never "ok".

## 5. Stop and clean up

```bash
V=$HOME/tmp/verify-apowerb-run
kill $(cat $V/server.pid) 2>/dev/null; pkill -P $(cat $V/server.pid) 2>/dev/null   # the Python child of `uv run`
sleep 2; ss -ltn | grep ':18000 ' && echo "STILL RUNNING"
docker rm -f verify-apowerb-pg
rm -rf $V
```

Never use `pkill -f` with a broad pattern: other `apowerb serve` processes may run on the machine,
and the command or ssh session that launched it would be killed too. Check that port 18000 is
free, that the container is gone (`docker ps -a | grep verify-apowerb`) and that nothing started
here is still running. If a worktree was created for the run: `git worktree remove` without
`--force`, after looking at `git status`.

## Rules

- At most three fix attempts on the same failure, then stop and report to whoever assigned the
  task, with the exact output.
- Flaky test: no retry loop, no `sleep`, no `skip`. Find the cause; otherwise report what was
  observed.
- Known pitfalls in this repository:
  - `DB_SSLMODE=disable` is required with an SSL-less Postgres (default `require`).
  - A valid `ENCRYPT_KEY` (32-byte Fernet key, url-safe base64) is required by the tests too.
  - Common Postgres ports (5432, 55432) are often taken: this skill uses 56432; check first.
  - Workflows live under `/api/workflows/defs` (create, validate, run) and their runs under
    `/api/workflows/runs`; `/api/workflows` alone does not exist (404).
  - The sign-up e-mail is validated: `.test` and `.example` are rejected.
  - Startup applies no manual migration: it creates missing tables in the target database, so
    never point it at a shared database.

## Last successful run

2026-11-17 — Linux + Docker — cheap checks (harness OK; 7000 tests passed in 3 min 48 s), then
throwaway Postgres 16 + API on 18000: health (live, ready, /health), sign-up, login (token
obtained), access with and without token (200/401), agent created, read and deleted, workflow
validated, created, run (SSE through `done` "ok bonjour Ada") and run listed as `success` — commit
`8e6b2c3` (origin/main on 2026-11-17). Not exercised: running an agent (LLM), wrong password
(401 expected), `BYPASS_AUTH`.
