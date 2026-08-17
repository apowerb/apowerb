# apowerb

Agentic framework for building and deploying AI agents: flexible orchestration, custom
tool integration, RAG, Text-to-SQL, webhooks and scheduled runs.

This repository is the **open-source core**. Some capabilities named in the product —
billing, usage metering, prospection, identity-provider sign-in, multi-factor
authentication, agent evaluation, the administration panel — ship as separate commercial
bricks and are **absent here**. Where the core holds a hook for one, it is documented as
such. A `404` on those routes means "not in this edition", not "object not found".

Full documentation: [docs.apowerb.com](https://docs.apowerb.com).

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running](#running)
- [CLI](#cli)
- [Architecture](#architecture)
- [API Reference](#api-reference)
- [Available Tools](#available-tools)
- [Integrations](#integrations)
- [Webhooks](#webhooks)
- [RAG (Retrieval-Augmented Generation)](#rag-retrieval-augmented-generation)
- [Text-to-SQL](#text-to-sql)
- [SSE Streaming](#sse-streaming)
- [Credits and billing](#credits-and-billing)
- [Scheduled runs](#scheduled-runs)
- [Agent Hub](#agent-hub)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

---

## Features

- **REST API** based on FastAPI with automatic OpenAPI documentation
- **Google ADK** (Agent Development Kit) for agent management and execution
- **LiteLLM** for multi-model compatibility (Anthropic, OpenAI, Mistral, Google, OVHcloud, etc.)
- **Multi-pattern orchestration**: base, parallel, sequential, loop
- **Sub-agents**: hierarchical agent composition
- **Modular tool system** with 31 tool modules (Google Workspace, Microsoft 365, databases, RAG, etc.)
- **RAG as a Service**: index files, URLs, databases, and S3 into knowledge bases
- **Text-to-SQL**: natural language to SQL query conversion
- **Webhooks**: Gmail (Pub/Sub) and Outlook (Graph API) push notifications to trigger agents
- **OAuth integrations**: GitHub, Google, Microsoft, LinkedIn
- **SSE streaming**: real-time agent responses, RAG progress, and notifications
- **Artifact generation**: agents can create and execute code files
- **Agent Hub**: publish and clone agents across organizations
- **Scheduled runs**: cron-based agent execution, driven by an external orchestrator
- **Supervision**: an auditable session list, scoped to what the caller may read
- **Revocable sessions**: a per-account cut-off that refuses tokens minted before it
- **Persistent sessions** with conversation context
- **PostgreSQL database** with auto-migrations
- **Encryption** for API keys, tokens, and sensitive data
- **Full CLI** for agent and server management

---

## Prerequisites

- Python 3.13+
- PostgreSQL
- UV (package manager)

---

## Installation

1. **Install UV and create virtual environment**:

```bash
pip install uv
uv venv
```

2. **Activate the virtual environment**:

```bash
# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate
```

3. **Install dependencies**:

```bash
uv sync
uv pip install .
```

4. **Configure environment**:

```bash
cp .env.example .env
```

---

## Configuration

### Required Variables

| Variable | Description |
|----------|-------------|
| `DB_HOST` | PostgreSQL database host |
| `DB_PORT` | Port (default: `5432`) |
| `DB_NAME` | Database name |
| `DB_USER` | Database username |
| `DB_PASSWORD` | Database password |
| `DB_SCHEMA` | Schema (default: `public`) |
| `ENCRYPT_KEY` | Encryption key for secrets and JWT signing |

### Optional Variables

#### Security & JWT

| Variable | Default | Description |
|----------|---------|-------------|
| `WORKING_MODE` | `development` | `development` or `production` |
| `ALGORITHM` | `HS256` | JWT signing algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `120` | JWT token lifetime |

#### CORS

| Variable | Default | Description |
|----------|---------|-------------|
| `FRONTEND_URLS` | `["http://localhost:3000"]` | Allowed frontend origins (JSON array) |

#### OAuth — User Login

| Variable | Description |
|----------|-------------|
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub OAuth (login) |
| `GITHUB_REDIRECT_URI` | GitHub login callback |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth (login) |
| `GOOGLE_REDIRECT_URI` | Google login callback |
| `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` | Microsoft OAuth (login) |
| `MICROSOFT_TENANT_ID` | Microsoft tenant (`common` for multi-tenant) |
| `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET` | LinkedIn OAuth (login) |

#### OAuth — Workspace Integrations

| Variable | Description |
|----------|-------------|
| `GOOGLE_INTEGRATION_CLIENT_ID` | OAuth app for Google Workspace (Drive, Gmail, Calendar, Sheets, Docs) |
| `GOOGLE_INTEGRATION_CLIENT_SECRET` | Google integration secret |
| `GOOGLE_INTEGRATION_REDIRECT_URI` | Google integration callback |
| `MICROSOFT_INTEGRATION_CLIENT_ID` | OAuth app for Microsoft 365 (Outlook, Teams, OneDrive, SharePoint) |
| `MICROSOFT_INTEGRATION_CLIENT_SECRET` | Microsoft integration secret |
| `MICROSOFT_INTEGRATION_TENANT_ID` | Microsoft integration tenant |
| `MICROSOFT_INTEGRATION_REDIRECT_URI` | Microsoft integration callback |
| `GITHUB_INTEGRATION_CLIENT_ID` | OAuth app for GitHub workspace integration |
| `GITHUB_INTEGRATION_CLIENT_SECRET` | GitHub integration secret |
| `GITHUB_INTEGRATION_REDIRECT_URI` | GitHub integration callback |

#### Gmail Pub/Sub Webhooks

| Variable | Description |
|----------|-------------|
| `GMAIL_PUBSUB_PROJECT_ID` | Google Cloud project ID |
| `GMAIL_PUBSUB_TOPIC` | Pub/Sub topic name (just the name, not the full path) |

#### RAG & Webhooks

| Variable | Default | Description |
|----------|---------|-------------|
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Public URL of this apowerb instance |
| `RAG_WEBHOOK_SECRET` | `th2-webhook-default-secret` | HMAC-SHA256 secret for RAG webhooks (change in production!) |
| `RAG_BASE_URL` | — | RAG API base URL (th2llm) |

#### Stripe (billing brick)

`settings` still declares these, and `user` still carries `stripe_customer_id`, but the
billing routes and the credit logic live in the **billing brick**. Setting them changes
nothing in a core-only install.

| Variable | Description |
|----------|-------------|
| `STRIPE_SECRET_KEY` | Stripe secret key |
| `STRIPE_PUBLISHABLE_KEY` | Stripe publishable key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |

#### S3 Storage (optional)

| Variable | Description |
|----------|-------------|
| `STORAGE_MODE` | `local` or `s3` |
| `S3_REGION` | AWS region |
| `S3_ACCESS_KEY` / `S3_ACCESS_KEY_SECRET` | S3 credentials |
| `S3_ENDPOINT` | S3 endpoint URL |
| `S3_BUCKET_NAME` | Bucket name |

#### Orchestrator (scheduled runs, optional)

Two backends drive scheduled runs. `ORCHESTRATOR` selects one; the code still defaults to
`mage`, the historical backend, so an instance that wants th2etl must say so explicitly.

| Variable | Default | Description |
|----------|---------|-------------|
| `ORCHESTRATOR` | `mage` | `mage` or `th2etl` |

With `ORCHESTRATOR=th2etl`:

| Variable | Default | Description |
|----------|---------|-------------|
| `TH2ETL_BASE_URL` | `http://localhost:8009` | th2etl instance |
| `TH2ETL_API_KEY` | — | Must equal the `API_KEY` of that th2etl instance — its business routes require `Authorization: Bearer <key>` |

With `ORCHESTRATOR=mage`:

| Variable | Default | Description |
|----------|---------|-------------|
| `BASE_URL` | `http://localhost:6789` | Mage AI scheduler URL |
| `API_KEY` | — | Mage API key |
| `OAUTH_TOKEN` | — | Mage OAuth token |
| `PROJECT_NAME` | `default_repo` | Mage project name |

#### Extensions

| Variable | Default | Description |
|----------|---------|-------------|
| `TH2_EXTENSIONS` | — | Comma-separated modules to load as bricks |

Installing a brick and loading it are two different steps: a package present in the
environment but absent from `TH2_EXTENSIONS` behaves exactly like one that was never
installed. Check the loaded set, not the installed set, when a capability seems missing.

<!-- Naming: the variable predates the rename from th2agent to apowerb and is read
     verbatim by the deploy workflow, so it keeps its name. -->


---

## Running

### Via CLI

```bash
apowerb serve
```

### Via Uvicorn

```bash
uv run uvicorn apowerb.main:app --reload
```

Server starts at **http://127.0.0.1:8000/**

`/docs`, `/redoc` and `/openapi.json` are **removed from the served routes by default**:
they hand the full route inventory to an unauthenticated caller, which is enough to
fingerprint a deployment from the outside. Opt in with `PUBLISH_API_SCHEMA=true` for a
browsable Swagger; `WORKING_MODE=production` overrides that flag as a second lock.

The published reference is at [docs.apowerb.com](https://docs.apowerb.com/api-reference).

---

## CLI

```bash
# Start server
apowerb serve --host 0.0.0.0 --port 8000

# Manage agents
apowerb agents list
apowerb agents create
apowerb agents delete <agent_id>

# Manage tools
apowerb tools list

# Manage runs
apowerb runs list
```

---

## Architecture

```
apowerb/
├── src/apowerb/
│   ├── main.py              FastAPI entry point, middleware, router mounting, startup
│   ├── models.py            SQLAlchemy ORM models
│   │
│   ├── auth/                Email/password login, JWT dependencies, session cut-off
│   ├── users/               User CRUD
│   ├── routers/             HTTP endpoints, one module per family
│   ├── core/                Business logic behind the routers
│   │   ├── adk_runner.py        ADK execution
│   │   ├── adk_agent_builder.py agent Python file generation
│   │   ├── extensions/         brick loader and registry hooks
│   │   ├── guardrails.py       input/output guardrails
│   │   ├── run_gate.py         admission control for runs
│   │   └── history_compaction.py  conversation trimming
│   ├── tools_store/         Tool registry + portfolio of tool modules
│   ├── skills_store/        Reusable agent skills
│   ├── agent_store/         Agent templates and seeds
│   ├── bi/                  Charts, dashboards, datasets
│   ├── sqlgen/              Text-to-SQL generation
│   ├── integrations/        OAuth workspace integrations
│   ├── artifacts/           Artifact storage and execution
│   ├── scheduler/           Orchestrator clients (th2etl, mage), background workers
│   ├── storage/             Local and S3 storage abstraction
│   ├── middleware/          Request middleware
│   ├── helpers/             Database, security, encryption, notification bus, migrations
│   ├── schema/              Pydantic schemas
│   ├── configs/             Settings and logging
│   └── cli/                 Typer CLI (`apowerb`)
│
├── agents_pool/             Generated agent code (runtime)
├── artifacts_store/         Artifacts written by agents (runtime, local mode)
├── uploads/                 Uploaded files (runtime, local mode)
├── tests/
└── pyproject.toml
```

Routers not to look for here — they arrive with the bricks: billing, usage, prospection,
identity-provider sign-in, MFA, evaluation, administration.

### Startup Sequence

On application start, apowerb:
1. Creates required directories (`agents_pool/`, `artifacts_store/`, `uploads/`)
2. Runs auto-migrations (`ensure_*` functions) for all database tables
3. Generates agent Python modules from database
4. Starts the webhook renewal background task (every 6 hours)
5. Mounts the Google ADK FastAPI sub-application

---

## API Reference

The full reference — every route, parameter and response shape — is generated from the
code and published at [docs.apowerb.com/api-reference](https://docs.apowerb.com/api-reference).
It is not duplicated here: a hand-maintained route table in this file went stale within
two weeks last time, and a stale reference is worse than none.

Route families in this edition, all under `/api` unless noted:

| Family | What it covers |
|--------|----------------|
| `auth` | Email/password login, refresh, logout, password reset, email verification |
| `users` | User CRUD, `me` |
| `agents` | Agent CRUD, reload, status, template resync, run |
| `adk` | ADK execution: run, streaming (`run_sse`), sessions, titles, traces |
| `tools`, `tools_config` | Tool registry and per-agent tool configuration |
| `skills`, `workflows` | Reusable skills, multi-step flows |
| `superagents` | Templates composing several agents |
| `rag` | Indexing files, URLs, databases, S3; status and progress stream |
| `artifacts` | Files produced by agents, listing, download, execution, library |
| `files` | Upload (including chunked), download, delete |
| `data-lake` | Pin storage read/write/list |
| `hub` | Publish and clone agents |
| `integrations` | OAuth workspace connections (Google, Microsoft, GitHub) |
| `emailing` | Outlook mail OAuth flow, shared mailboxes |
| `webhooks` | Subscription CRUD and inbound push dispatch |
| `notifications` | User notifications and SSE stream |
| `supervision` | Session list for audit, scoped to what the caller may read |
| `scheduler` | Scheduled runs through the configured orchestrator |
| `charts`, `dashboards`, `bi-*` | BI: charts, dashboards, datasets, refresh, stats |
| `share` | Shareable conversation links |
| `api-keys` | Saved provider keys |
| `config`, `health` | Public configuration, liveness |

Absent from this edition, provided by bricks: `billing`, `usage`, `prospection`,
`campaigns`, MFA (`/api/auth/mfa/*`), identity-provider sign-in (`/api/users/{github,google,microsoft,linkedin}`),
evaluation, administration. They answer `404` here.

## Available Tools

### Google Workspace
| Tool Module | Functions |
|-------------|-----------|
| `google_drive` | List, search, read, download files |
| `google_gmail` | List, search, read, send emails |
| `google_calendar` | List, search, create events |
| `google_sheets` | Get spreadsheet info, read, write cells |
| `google_docs` | Create, read, append to documents |

### Microsoft 365
| Tool Module | Functions |
|-------------|-----------|
| `outlook_mail` | List, search, read, send emails, list folders, download attachments |
| `onedrive` | List, search, read, download, upload files, create folders, delete, shared files |
| `teams` | List chats, get/send messages, reply, search, create group chats, list members |

### Data & Database
| Tool Module | Functions |
|-------------|-----------|
| `database` | Generic database queries, SQL execution |
| `text_to_sql` | Natural language to SQL, schema introspection |
| `db_to_rag` | Index database query results into RAG |
| `data_handler` | Data filtering, aggregation, transformation |

### RAG & Knowledge
| Tool Module | Functions |
|-------------|-----------|
| `rag` | Create, list, search, delete knowledge bases |
| `memory` | Save/search user memory, tool usage patterns |

### Communication
| Tool Module | Functions |
|-------------|-----------|
| `emailing` | Send emails (SMTP) |
| `marketing` | HubSpot integration, sales leads |

### Utilities
| Tool Module | Functions |
|-------------|-----------|
| `basic` | Read files, data loader, image conversion, bearer tokens |
| `api_call` | Generic API calls, Thaink² forecast |
| `s3_tools` | Read S3 PDFs, search S3 files |
| `visualization` | Chart generation, CSV to chart export |
| `web_search_mcp` | Web search via MCP |
| `thaink2` | Custom Thaink² RAG tools |

---

## Integrations

### OAuth Providers

apowerb supports two categories of OAuth:

1. **User Login** — Sign up / log in via OAuth (GitHub, Google, Microsoft, LinkedIn)
2. **Workspace Integrations** — Connect external services as tools for agents

#### Connecting a Workspace Integration

1. User calls `GET /api/integrations/{provider}/{service}/connect` to get the OAuth URL
2. User authorizes the app on the provider's consent screen
3. Provider redirects to `POST /api/integrations/{provider}/callback`
4. apowerb stores the access/refresh tokens encrypted in the `integrations` table
5. Agent tools can now use the integration tokens

#### Supported Services

| Provider | Services | Scopes |
|----------|----------|--------|
| **Google** | Drive, Gmail, Calendar, Sheets, Docs | Service-specific (e.g., `gmail.readonly`, `drive.readonly`) |
| **Microsoft** | Outlook, Teams, OneDrive, SharePoint | Service-specific (e.g., `Mail.Read`, `Files.ReadWrite`) |
| **GitHub** | Repositories, Gists | `repo`, `gist`, `read:user` |

---

## Webhooks

apowerb can automatically trigger agents when events occur in connected services (new email, etc.). Two providers are supported: **Gmail** (via Google Pub/Sub) and **Outlook** (via Microsoft Graph subscriptions).

### How It Works

```
Email arrives → Provider pushes notification → apowerb receives it
     → Fetches new email content → Runs associated agent → Logs result
```

### Webhook Subscription API

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/webhooks/subscriptions` | Create subscription |
| GET | `/api/webhooks/subscriptions` | List subscriptions |
| PATCH | `/api/webhooks/subscriptions/{id}` | Update (agent, template, change_type) |
| DELETE | `/api/webhooks/subscriptions/{id}` | Delete and unsubscribe |
| POST | `/api/webhooks/subscriptions/{id}/renew` | Manually renew |
| GET | `/api/webhooks/logs` | View execution logs |

**Create a subscription:**

```json
POST /api/webhooks/subscriptions
{
  "provider": "google_gmail",
  "resource": "INBOX",
  "agent_id": "AGENT_ID",
  "change_type": "created",
  "agent_message_template": "New email from {{ sender }}: {{ subject }}\n\n{{ body }}"
}
```

### Automatic Renewal

- **Gmail watches** expire after 7 days
- **Outlook subscriptions** expire after 3 days
- A background task runs every **6 hours** and renews subscriptions expiring within 12 hours

### Gmail Webhook Setup (Pub/Sub)

#### Architecture

```
Gmail  ──push──▶  Google Pub/Sub  ──HTTP POST──▶  apowerb
                  (topic)                         /api/webhooks/gmail/notifications
                                                        │
                                                        ▼
                                                  Fetch new emails via Gmail API
                                                        │
                                                        ▼
                                                  Run associated agent
```

#### Prerequisites

- A **Google Cloud project** with billing enabled
- **Gmail API** and **Cloud Pub/Sub API** enabled
- A **Google OAuth 2.0 application** for workspace integration
- A **publicly accessible URL** for apowerb

#### Step 1 — Enable Google APIs

```bash
gcloud services enable gmail.googleapis.com pubsub.googleapis.com \
  --project=YOUR_PROJECT_ID
```

#### Step 2 — Create a Pub/Sub Topic

```bash
gcloud pubsub topics create gmail-notifications \
  --project=YOUR_PROJECT_ID
```

#### Step 3 — Grant Gmail Permission to Publish

Gmail uses the service account `gmail-api-push@system.gserviceaccount.com` to push notifications. Grant it the **Pub/Sub Publisher** role:

```bash
gcloud pubsub topics add-iam-policy-binding gmail-notifications \
  --project=YOUR_PROJECT_ID \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

> **Note:** In the Google Cloud Console UI, the "Publisher" role may not appear in the dropdown by default — type "publisher" in the search bar to find it, or use the CLI command above.

#### Step 4 — Create a Push Subscription

```bash
gcloud pubsub subscriptions create gmail-notifications-push \
  --topic=gmail-notifications \
  --push-endpoint=https://YOUR_DOMAIN/api/webhooks/gmail/notifications \
  --project=YOUR_PROJECT_ID
```

#### Step 5 — Configure OAuth 2.0

In [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials):

1. Create an **OAuth 2.0 Client ID** (type: Web application)
2. Add authorized redirect URI: `https://YOUR_DOMAIN/integrations/google/callback`
3. Add scope: `https://www.googleapis.com/auth/gmail.readonly`

#### Step 6 — Set Environment Variables

```bash
GOOGLE_INTEGRATION_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_INTEGRATION_CLIENT_SECRET=GOCSPX-xxxxxxxxxx
GOOGLE_INTEGRATION_REDIRECT_URI=https://YOUR_DOMAIN/integrations/google/callback

GMAIL_PUBSUB_PROJECT_ID=your-gcp-project-id
GMAIL_PUBSUB_TOPIC=gmail-notifications

PUBLIC_BASE_URL=https://YOUR_DOMAIN
```

#### Step 7 — Connect Gmail Account

In the apowerb-ui UI: **Integrations → Connect Google (Gmail)**

#### Step 8 — Create Webhook Subscription

Via UI (Webhook Manager) or API:

```bash
curl -X POST https://YOUR_DOMAIN/api/webhooks/subscriptions \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "google_gmail",
    "resource": "INBOX",
    "agent_id": "AGENT_ID",
    "change_type": "created"
  }'
```

### Outlook Webhook Setup

#### Prerequisites

- A registered **Microsoft Azure AD application**
- **Microsoft Graph API** permissions: `Mail.Read`
- A publicly accessible HTTPS URL

#### Setup

1. Configure `MICROSOFT_INTEGRATION_*` environment variables
2. Connect Outlook integration in the UI
3. Create a webhook subscription with `"provider": "microsoft_outlook"`

The Outlook handler uses Microsoft Graph change notifications with `clientState` validation.

### Webhook Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `403 — User not authorized` | Gmail can't publish to Pub/Sub topic | Grant `gmail-api-push@system.gserviceaccount.com` the Publisher role on your topic |
| `404 — Topic not found` | Topic missing or wrong project | Verify `GMAIL_PUBSUB_PROJECT_ID` and `GMAIL_PUBSUB_TOPIC` |
| Notifications not arriving | Push subscription misconfigured | Verify the push endpoint URL is correct and publicly accessible |
| `401 — Invalid credentials` | OAuth token expired | Reconnect the integration from the UI |
| Watch expired | Auto-renewal failed | Check scheduler logs, manually renew via API |

---

## RAG (Retrieval-Augmented Generation)

RAG lets agents answer questions based on documents you provide — PDFs, web pages, database exports, or S3 files.

### How It Works

```
Upload documents → Documents get indexed (via th2llm) → Agent searches them when answering
```

### Indexing Sources

| Source | Endpoint | Description |
|--------|----------|-------------|
| Files | `POST /api/rag/index-files` | Upload PDF, CSV, TXT, DOCX, etc. (max 50 MB/file) |
| URL | `POST /api/rag/index-url` | Crawl and index a web page |
| Database (SQL) | `POST /api/rag/index-db` | Index query results using a SQL query |
| Database (NL) | `POST /api/rag/index-db-nl` | Index query results using natural language |
| S3 | `POST /api/rag/index-s3` | Index files from an S3 bucket |

### Tracking Progress

- **Poll**: `GET /api/rag/status/{knowledge_id}`
- **Stream (SSE)**: `GET /api/rag/stream/{agent_id}?session_id=xxx`

### Security

- Path traversal prevention on session/agent IDs
- SSRF protection on URL indexing (blocks localhost, private IPs)
- Session ownership validation
- HMAC-SHA256 signature verification on th2llm webhooks

---

## Text-to-SQL

Agents can connect to relational databases and convert natural language questions into SQL queries.

1. Create a database tool config with connection parameters
2. Create an agent with `text_to_sql` tool enabled
3. Ask questions — the agent introspects the schema, generates SQL, executes it, and returns results

---

## SSE Streaming

Three SSE streaming channels are available:

| Channel | Endpoint | Events |
|---------|----------|--------|
| **Agent response** | `POST /api/adk/run_sse` | Token-by-token agent output |
| **RAG progress** | `GET /api/rag/stream/{agent_id}` | `connected`, `status`, `complete` |
| **Notifications** | `GET /api/notifications/stream` | Real-time user notifications |

---

## Credits and billing

Not in this edition. The `user` row carries a credit balance and `transactions` /
`credit_purchases` exist in the schema, but the packages, the Stripe checkout and the
crediting logic belong to the **billing brick**. `/api/billing/*` answers `404` here.

The `llm_usage` table is declared here, but **nothing in the core writes to it**: the
metering is the usage brick's job. A core-only install has the table and leaves it empty.

---

## Scheduled runs

Agent runs can be automated on a schedule. The core does not run the cron itself: it
registers the schedule with an external orchestrator, selected by `ORCHESTRATOR`
(`th2etl`, or the historical `mage` which is still the code default).

```json
POST /api/adk/schedule_run
{
  "agent_id": "agent42",
  "user_id": "user@example.com",
  "session_id": "session_scheduled",
  "new_message": {
    "role": "user",
    "parts": [{ "text": "Generate the daily report" }]
  },
  "schedule_interval": "@daily",
  "start_time": "2026-03-10T08:00:00"
}
```

### Schedule Presets

| Preset | Cron |
|--------|------|
| `@hourly` | `0 * * * *` |
| `@daily` | `0 0 * * *` |
| `@weekly` | `0 0 * * 0` |
| `@monthly` | `0 0 1 * *` |
| Custom | e.g., `*/15 * * * *` |

---

## Agent Hub

Agents can be published to a shared Hub and cloned by other users:

- `POST /api/hub/publish` — Publish an agent (copies config, tools, instructions)
- `POST /api/hub/clone` — Clone a Hub agent into your workspace
- `GET /api/hub` — Browse available agents

---

## Database Models

| Table | Description | Key Fields |
|-------|-------------|------------|
| `user` | User accounts | email, password, role, credits, stripe_customer_id, OAuth IDs, `mfa_enabled`, `mfa_required`, `sessions_valid_from` |
| `integrations` | Connected OAuth services | user_id, provider, access_token, refresh_token, scopes, meta |
| `webhook_subscriptions` | Active webhook watches | user_id, integration_id, provider, subscription_id, resource, agent_id, status, expiration |
| `webhook_logs` | Webhook execution history | subscription_id, trigger_event, email_subject, agent_response, duration_ms, status |
| `notifications` | User notifications | user_id, title, message, type (webhook/info/warning/error), is_read |
| `transactions` | Credit movements | user_id, type (purchase/usage/refund/bonus), amount, balance_after, stripe IDs |
| `credit_purchases` | Stripe purchase sessions | user_id, stripe_session_id, credits_amount, price_paid, status |
| `llm_usage` | Per-call token accounting | agent_id, owner_id, invocation_id, model, token counts, created_at |
| `business_intelligence` | BI charts, dashboards, datasets | owner, definition, refresh state |
| `shared_conversations` | Shareable conversation links | share_id, session, owner |
| `oauth_states` | Short-lived OAuth state tokens | state, provider, expiry |

Sessions, events and ADK artifacts live in the tables Google ADK owns (`sessions`,
`events`), which supervision reads directly rather than fanning out one HTTP call per
agent.

Some columns here serve bricks rather than the core: `credits`, `stripe_customer_id`,
`mfa_*` and the `transactions` / `credit_purchases` / `llm_usage` tables are declared so a
brick can use them, and stay untouched without one.

---

## Supported Models

Via LiteLLM, all major model providers are supported:

- **Anthropic**: `anthropic/claude-sonnet-4-5-20250929`, `anthropic/claude-3-haiku-20240307`
- **OpenAI**: `openai/gpt-4o`, `openai/gpt-4`, `openai/gpt-3.5-turbo`
- **Mistral**: `mistral/mistral-large-latest`
- **Google**: `gemini/gemini-pro`
- **OVHcloud**: `ovhcloud/DeepSeek-R1-Distill-Llama-70B`
- And more...

---

## Development

```bash
# Install development dependencies
uv sync --group dev

# Run tests
pytest

# Linting
ruff check .
ruff format .
```

---

## Troubleshooting

### Common Errors

| Code | Meaning |
|------|---------|
| `400` | Bad request — check your request body |
| `401` | Unauthorized — missing or invalid token |
| `403` | Forbidden — you don't own this resource |
| `404` | Not found — agent, session, or document doesn't exist |
| `413` | File too large — max 50 MB per file |
| `422` | Unprocessable — e.g., query returned no data |
| `500` | Internal server error |
| `503` | Service temporarily unavailable |

### Detailed API Guide

Guides, quickstart and the generated API reference are at
[docs.apowerb.com](https://docs.apowerb.com). Their source lives in
[apowerb/apowerb-docs](https://github.com/apowerb/apowerb-docs).

---

## Support

For questions or contributions, contact the thaink² team.

## License

Proprietary - thaink² 2025
