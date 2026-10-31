# Webhook backlog — quota-scoped cooldown

## Purpose
Protect an exhausted provider quota without blocking ready work known to use
an independent quota, and without spending retries on paused work.

## Where the code is
- `src/apowerb/scheduler/backlog_worker.py` — ready-row selection and retry lifecycle.
- `src/apowerb/scheduler/backlog_quota.py` — per-process quota deadlines.
- `src/apowerb/configs/settings.py` — `backlog_quota_groups` / `BACKLOG_QUOTA_GROUPS`.
- Tests: `tests/test_backlog_worker.py`, `tests/test_backlog_quota.py`.

## Business rules
1. The deployment operator can supply a JSON mapping from webhook agent ids
   to non-secret shared quota groups, for example
   `BACKLOG_QUOTA_GROUPS={"42":"project-a","43":"project-a","44":"project-b"}`.
   No mapping is required: the default `{}` retains the global cooldown.
2. A group represents **all** upstream quotas a workflow can consume, including
   sub-agents and tools. Agents sharing any limit must share one group, even
   across tenants. If several quotas overlap, group their connected workflows
   together. API-key, model and tenant differences alone are not evidence of
   independent provider limits. Unknown or ambiguous scope must stay unmapped.
3. A 429 pauses its group. Only explicitly mapped, unaffected agents can be
   claimed during a known group's cooldown. Unmapped agents wait because
   independence is unknown; a 429 from an unmapped/blank group pauses everyone.
4. Filtering happens in the database before claiming, so a paused row ahead
   in FIFO order does not block independent work or increment its attempts.
5. A subsequent 429 can extend its group's deadline, never shorten it or
   clear another group's deadline. Even the final failed attempt protects
   remaining work while its own row is marked as exhausted.
6. Configuration is operator-owned, not read from an untrusted webhook payload.
   Do not put API keys in group names. No group labels or credentials are logged
   or added to queue metadata. Restart workers when this mapping or the relevant
   agent/provider configuration changes; stale declarations cannot establish
   independence safely.

## States and transitions
Ready row → claimed/in progress → success, retrying or exhausted, as before.
On 429, retain the provider retry hint plus the existing safety margin and set
that group's deadline. Expiry makes its ready work eligible again without a
manual reset. Paused rows retain their status and attempt count.

## Functional edge cases
- Shared quota across different tenants; independent work behind a blocked row.
- Unknown/blank scope, including jobs without an agent id: conservative pause.
- Distinct expiries, repeated 429s, exhausted attempt budget and retry resumption.
- Existing claim races, stale reclaim and notification deduplication remain covered.
- Limits: cooldowns are in memory per worker process, not coordinated across
  replicas or persisted across restarts. The mapping is a deployment assertion,
  not automatic provider-quota discovery. This change does not implement the
  effects ledger or make replay of an uncertain external action safe (#466).

## Checks
`DB_SCHEMA="" uv run pytest -q tests/test_backlog_worker.py tests/test_backlog_quota.py`

Also run `python3 scripts/harness.py` and the repository's non-integration suite
and pre-commit checks; retain actual command output in the PR.
