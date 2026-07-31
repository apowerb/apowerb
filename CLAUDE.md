# apowerb — product vision and working frame

> Read before any decision about architecture, packaging or public API.
> Source: Farid ↔ David meeting, 2026-07-28. Code state verified 2026-07-31.

## 1. The model: open core

thaink2 sells **bespoke work** (platform plus a custom business component — the SCEI model),
aimed at small and mid-sized companies. apowerb is not the product being sold: it is **the
visibility lever**, and the tool that makes bespoke delivery fast.

Two entry doors to build, in this order:

| Priority | Door | Contents | Business model |
|---|---|---|---|
| **1** | **Get started** — hosted Studio | the app as it stands, SaaS | freemium: pooled LLM bought around €5/M tokens, resold around €10/M, entry coupon around €20 |
| **2** | **Self-host** | PyPI + Docker images + Helm chart + docker-compose + CLI + docs | open source, stated reference: **n8n** (community edition) |

**The Studio first.** Finish it, open it as freemium, communicate. Self-hosting comes next —
Farid calls it "the most challenging piece".

## 2. The open source / commercial line

The n8n model: the community edition is complete **except for the enterprise part**.

- **Stays commercial**: SSO / SAML / LDAP, the logs part, the usage and consumption part.
  (in Farid's words: "the version without advanced auth")
- **Goes open source**: the agentic core, the tools, the ETL, observability, RAG.

⚠️ **Direct consequence for the code**: anything touching advanced auth, logging or usage
must live in a brick the core can run without. The core never names a brick — it finds them
through the extension registry, which is what makes the commercial code *absent* from this
repository rather than merely disabled.

Two environment variables, two distinct jobs, and confusing them is silent:
`UV_EXTRAS` **installs** the bricks, `TH2_EXTENSIONS` **loads** them. Forgetting the second
yields a service that starts, answers 200 on `/health`, and has lost billing, token
accounting, prospecting and identity-provider login — without a single error in the logs.

## 3. Monetisation — decided in part

Farid had not arbitrated between selling **tokens**, selling **credit**, or also monetising
**feature and tool usage**; that discussion with Anis still stands for the pricing shape.

What is now wired: purchased credits raise the usage ceiling, and the run guards apply on
every entry point. Consumption is counted **per month**, backed by Stripe.

## 4. Actual state of the code (verified 2026-07-31)

- **Token quota** — `core/usage_quota.py`, `helpers/quota_guard.py`. Per user, in tokens,
  calendar month Europe/Paris, clean 402 `QUOTA_EXCEEDED` refusal before the flow starts.
  Ceiling: `DEFAULT_LLM_MONTHLY_TOKEN_QUOTA` (1,000,000 by default), overridable per plan
  through `DEFAULT_LLM_PLAN_QUOTAS`.
- **Accounting** — `llm_usage` plus `helpers/model_pricing.py` (⚠️ cached input is billed at
  10% of input). Documented as best-effort: a guard rail, **not the source of truth for an
  invoice**.
- **Stripe** — `billing/` is complete: credit packs, checkout, balance, transactions,
  customer portal, signed webhook.
- **Packaging** — this repository is published on PyPI as `apowerb`, and the split is done:
  the core here, the commercial bricks in `thaink2/apowerb`, the customer overlay in
  `th2customers/scei`.

The gap Farid identified in the meeting — a Stripe purchase credited `User.credits` while
`resolve_quota()` read `User.plan`, with nothing connecting them — is closed.

What still does **not** exist: Dockerfile, docker-compose, Helm chart, self-host config
repository, installation documentation.

## 5. Rules for any session working here

1. **This repository is public.** Every default value is a known credential the moment it
   ships, and every published name is permanent. A setting that guards an unauthenticated
   endpoint must fail closed, not warn and continue.
2. **Any new API or module is a future public artefact.** Naming, signatures and defaults
   commit you. No customer-specific hard-coded value in the core (see SCEI: the business
   logic lives in the template and the database, not in the app).
3. **Importing must stay inert.** No config required at import time, no DDL at module level,
   no dependency on the current working directory. The `build.yaml` CI installs and imports
   the wheel in a bare virtualenv under `env -i` — do not work around it.
4. **Licence: MIT**, aligned with th2etl and th2pulse. Farid mentioned "taking inspiration
   from n8n" — but n8n is not MIT, it is under a *sustainable use* licence. The contradiction
   has not been settled; do not change the licence without an explicit call from Farid.
5. The reference deployment is `iac/deploy.sh` on a VM, in `thaink2/apowerb`. The unit it
   writes carries the module path as a **literal string**: a package rename that forgets that
   line takes the service down. Verifying that a package imports proves nothing about the
   service that invokes it.
6. Work happens on **DAVE_OVH_VM** (worktrees plus the `.venv`), not locally. Measure the
   test baseline before and after a change rather than comparing against a remembered
   absolute number — `tests/test_audio_stream_router.py` (freezes on a futex) and
   `tests/test_integration_auth.py` (does not collect through `PYTHONPATH=src`) are
   pre-existing anomalies to exclude.

## 6. Ecosystem

| Component | GitHub | PyPI | Role |
|---|---|---|---|
| `apowerb` | **public**, MIT | ✅ **published** | agentic core (this repository) |
| `apowerb-ui` | **public** | npm `@apowerb/apowerb-sdk` ✅ | Next.js front end and its JavaScript SDK |
| `thaink2/apowerb` | private | ❌ | commercial bricks and the deployment IaC |
| `th2customers/scei` | private | ❌ | customer overlay |
| `th2pulse` | **public**, MIT | ❌ | OTel observability |
| `th2etl` | **public** | ✅ **published** | ETL and orchestration |
| `th2rag` | **public** | ✅ **published** | RAG |

Community to come: a Discord server (created by Farid) plus GitHub issues, with Discord,
GitHub Enterprise, Docker Hub and PyPI subscriptions still to be taken. External pull
requests are accepted or refused at the team's discretion.
