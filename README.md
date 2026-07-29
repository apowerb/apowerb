# thaink² Agentic

Agentic framework for building and deploying AI agents with flexible orchestration, custom tool integration, RAG, Text-to-SQL, webhooks, and billing.

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
- [Billing (Stripe)](#billing-stripe)
- [Scheduler](#scheduler)
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
- **Modular tool system** with 24+ tool modules (Google Workspace, Microsoft 365, databases, RAG, etc.)
- **RAG as a Service**: index files, URLs, databases, and S3 into knowledge bases
- **Text-to-SQL**: natural language to SQL query conversion
- **Webhooks**: Gmail (Pub/Sub) and Outlook (Graph API) push notifications to trigger agents
- **OAuth integrations**: GitHub, Google, Microsoft, LinkedIn
- **SSE streaming**: real-time agent responses, RAG progress, and notifications
- **Billing**: Stripe-based credit system with packages and transaction tracking
- **Artifact generation**: agents can create and execute code files
- **Agent Hub**: publish and clone agents across organizations
- **Scheduler**: cron-based agent execution via Mage AI
- **MFA**: TOTP-based multi-factor authentication
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
| `TEST_TOKEN` | Authentication token for development |

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
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Public URL of this th2agent instance |
| `RAG_WEBHOOK_SECRET` | `th2-webhook-default-secret` | HMAC-SHA256 secret for RAG webhooks (change in production!) |
| `RAG_BASE_URL` | — | RAG API base URL (th2llm) |

#### Stripe Billing

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

#### Scheduler / Mage (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `BASE_URL` | `http://localhost:6789` | Mage AI scheduler URL |
| `API_KEY` | — | Mage API key |
| `OAUTH_TOKEN` | — | Mage OAuth token |
| `PROJECT_NAME` | `default_repo` | Mage project name |

---

## Running

### Via CLI

```bash
th2agent serve
```

### Via Uvicorn

```bash
uv run uvicorn th2agent.main:app --reload
```

Server starts at **http://127.0.0.1:8000/**

API Documentation: **http://127.0.0.1:8000/docs**

---

## CLI

```bash
# Start server
th2agent serve --host 0.0.0.0 --port 8000

# Manage agents
th2agent agents list
th2agent agents create
th2agent agents delete <agent_id>

# Manage tools
th2agent tools list

# Manage runs
th2agent runs list
```

---

## Architecture

```
th2agent/
├── src/th2agent/
│   ├── main.py                     # FastAPI entry point, middleware, startup
│   ├── models.py                   # SQLAlchemy ORM models
│   │
│   ├── auth/                       # Authentication & MFA
│   │   ├── router.py               # Login, MFA, token refresh
│   │   ├── service.py              # Auth business logic
│   │   ├── dependencies.py         # JWT verification, user extraction
│   │   ├── mfa_service.py          # TOTP/MFA logic
│   │   └── schemas.py              # Auth request/response models
│   │
│   ├── users/                      # User management
│   │   ├── router.py               # User CRUD, OAuth callbacks
│   │   ├── service.py              # User business logic
│   │   └── oauth/                  # OAuth providers (GitHub, Google, Microsoft, LinkedIn)
│   │
│   ├── routers/                    # API endpoints
│   │   ├── agents.py               # Agent CRUD
│   │   ├── tools.py                # Tools & tool configs
│   │   ├── adk_runner.py           # ADK execution, sessions, streaming
│   │   ├── runs.py                 # Agent run status
│   │   ├── rag.py                  # RAG indexing & status
│   │   ├── webhooks.py             # Webhook subscription CRUD & dispatch
│   │   ├── integrations.py         # OAuth integration setup
│   │   ├── notifications.py        # User notifications & SSE stream
│   │   ├── hub.py                  # Agent Hub (publish, clone)
│   │   ├── artifacts.py            # Session artifact retrieval
│   │   ├── files.py                # File upload/download
│   │   ├── data_lake.py            # Data pin storage (S3)
│   │   ├── api_keys.py             # Saved API key management
│   │   ├── scheduler.py            # Mage trigger CRUD
│   │   ├── superagents.py          # SuperAgent templates
│   │   ├── emailing.py             # Outlook Mail OAuth flow
│   │   ├── google_drive_browser.py # Google Drive file browser
│   │   ├── onedrive_browser.py     # OneDrive file browser
│   │   ├── config.py               # Public config endpoint
│   │   └── webhook_handlers/       # Push notification handlers
│   │       ├── gmail.py            # Gmail Pub/Sub handler
│   │       ├── outlook.py          # Outlook Graph handler
│   │       └── _common.py          # Shared webhook logic
│   │
│   ├── billing/                    # Stripe billing
│   │   ├── router.py               # Checkout, balance, transactions
│   │   ├── stripe_service.py       # Stripe API integration
│   │   ├── packages.py             # Credit package definitions
│   │   └── webhooks.py             # Stripe webhook handler
│   │
│   ├── integrations/               # Third-party integrations
│   │   ├── google.py               # Google OAuth (Drive, Gmail, Calendar, Sheets, Docs)
│   │   ├── microsoft.py            # Microsoft Graph OAuth (Outlook, Teams, OneDrive, SharePoint)
│   │   ├── github.py               # GitHub OAuth
│   │   ├── slack.py                # Slack integration
│   │   ├── gmail_webhook.py        # Gmail Pub/Sub watch service
│   │   └── outlook_webhook.py      # Outlook webhook service
│   │
│   ├── core/                       # Core business logic
│   │   ├── agent_main.py           # Agent CRUD logic
│   │   ├── adk_runner.py           # ADK agent execution
│   │   ├── adk_agent_builder.py    # Agent Python file generation
│   │   ├── hub_main.py             # Hub agent publishing
│   │   ├── knowledge_map.py        # RAG knowledge source tracking
│   │   ├── rag_streaming.py        # RAG SSE streaming
│   │   ├── artifact_executor.py    # Artifact code execution
│   │   └── superagents.py          # SuperAgent templates
│   │
│   ├── tools_store/                # Tool management
│   │   ├── tool_manager.py         # Tool registry
│   │   └── portfolio/              # 24 tool modules (see Available Tools)
│   │
│   ├── scheduler/                  # Background tasks
│   │   ├── mage.py                 # Mage AI client
│   │   ├── webhook_renewal.py      # Webhook auto-renewal (every 6h)
│   │   └── run_agent_background.py # Background agent execution
│   │
│   ├── helpers/                    # Utilities
│   │   ├── database.py             # SQLAlchemy Base, session factory
│   │   ├── security.py             # JWT, password hashing
│   │   ├── encryptor.py            # Field encryption
│   │   ├── notification_bus.py     # SSE notification broadcasting
│   │   ├── pagination.py           # Pagination helpers
│   │   └── *_migration.py          # Auto-migration scripts
│   │
│   ├── schema/                     # Pydantic schemas
│   ├── configs/                    # Settings & logging
│   ├── storage/                    # S3 storage abstraction
│   └── cli/                        # CLI commands (Typer)
│
├── agents_pool/                    # Generated agent code (runtime)
├── pyproject.toml
├── .env
└── readme/                         # Detailed API guide with screenshots
```

### Startup Sequence

On application start, th2agent:
1. Creates required directories (`agents_pool/`, `artifacts_store/`, `uploads/`)
2. Runs auto-migrations (`ensure_*` functions) for all database tables
3. Generates agent Python modules from database
4. Starts the webhook renewal background task (every 6 hours)
5. Mounts the Google ADK FastAPI sub-application

---

## API Reference

### Health

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/health` | No | Health check |

### Authentication

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/auth/token` | No | Login (email + password) |
| POST | `/api/auth/refresh-token` | No | Refresh JWT token |
| POST | `/api/auth/logout` | Yes | Clear refresh token cookie |
| POST | `/api/auth/mfa/setup` | Yes | Generate TOTP secret + QR code |
| POST | `/api/auth/mfa/enable` | Yes | Activate MFA |
| POST | `/api/auth/mfa/verify` | No | Complete login with MFA code |
| POST | `/api/auth/mfa/disable` | Yes | Turn off MFA |
| GET | `/api/auth/mfa/status` | Yes | Check MFA status |
| GET | `/api/auth/mfa/backup-codes` | Yes | Retrieve backup codes |

### Users

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/users` | No | Create user account |
| GET | `/api/users` | Admin | List all users |
| GET | `/api/users/me` | Yes | Get current user profile |
| GET | `/api/users/{user_id}` | Yes | Get user by ID |
| PATCH | `/api/users/{user_id}` | Yes | Update user profile |
| DELETE | `/api/users/{user_id}` | Yes | Delete user |
| POST | `/api/users/oauth/{provider}/callback` | No | OAuth login (github, google, microsoft, linkedin) |

### Agents

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/agents` | Yes | List user's agents |
| POST | `/api/agents` | Yes | Create agent |
| GET | `/api/agents/{agent_id}` | Yes | Get agent details |
| PUT | `/api/agents/{agent_id}` | Yes | Update agent |
| DELETE | `/api/agents/{agent_id}` | Yes | Delete agent |
| POST | `/api/agents/{agent_id}/run` | Yes | Start agent run (background) |
| GET | `/api/agents/{agent_id}/status` | Yes | Get run status |

**Create an agent:**

```json
POST /api/agents
{
  "agent_name": "My Assistant",
  "agent_model": "anthropic/claude-sonnet-4-5-20250929",
  "agent_description": "Specialized agent for data analysis",
  "agent_instruction": "You are an expert data analyst.",
  "agent_type": "llm",
  "agent_tools": ["tool_config_id_1", "tool_config_id_2"],
  "sub_agents": [],
  "memory_enabled": false,
  "artifacts_enabled": false,
  "tags": ["analytics"]
}
```

**Agent types:**

| Type | Description |
|------|-------------|
| `llm` / `base` | Simple agent, direct execution |
| `parallel` | Sub-agents executed in parallel |
| `sequential` | Sub-agents executed in sequence |
| `loop` | Iterative execution |

### Sessions & Runs (ADK)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/adk/sessions` | Yes | Create conversation session |
| GET | `/api/adk/sessions/list` | Yes | List all sessions |
| GET | `/api/adk/sessions/{agent}/{user}/{session}` | Yes | Get conversation history |
| GET | `/api/adk/sessions/{agent}/{user}/{session}/trace` | Yes | Get agent execution trace |
| PATCH | `/api/adk/sessions/{agent}/{user}/{session}` | Yes | Update session state |
| DELETE | `/api/adk/sessions/{agent}/{user}/{session}` | Yes | Delete session |
| POST | `/api/adk/run` | Yes | Run agent (blocking) |
| POST | `/api/adk/run_sse` | Yes | Run agent (SSE streaming) |
| POST | `/api/adk/schedule_run` | Yes | Schedule recurring run |
| POST | `/api/adk/run_now` | Yes | Trigger immediate run |

**Run an agent:**

```json
POST /api/adk/run
{
  "agent_name": "My Assistant",
  "user_id": "user@example.com",
  "session_id": "session_001",
  "new_message": {
    "role": "user",
    "parts": [{ "text": "What is the forecast for next month?" }]
  },
  "run_mode": "single",
  "streaming": false
}
```

### Tools

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/tools` | Yes | List all available tools |
| GET | `/api/tools/{tool_name}/params` | Yes | Get tool config parameters |
| GET | `/api/tools_config` | Yes | List user's tool configs |
| GET | `/api/tools_config/{id}` | Yes | Get tool config details |
| POST | `/api/tools_config` | Yes | Create tool config |
| DELETE | `/api/tools_config/{id}` | Yes | Delete tool config |
| GET | `/api/mcp_configs` | Yes | List MCP server configs |

### RAG

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/rag/index-files` | Yes | Upload and index files |
| POST | `/api/rag/index-url` | Yes | Index a web URL |
| POST | `/api/rag/index-db` | Yes | Index database results (SQL query) |
| POST | `/api/rag/index-db-nl` | Yes | Index database results (natural language) |
| POST | `/api/rag/index-s3` | Yes | Index files from S3 |
| GET | `/api/rag/status/{knowledge_id}` | Yes | Check indexing status |
| GET | `/api/rag/knowledge/{agent_id}` | Yes | List indexed sources |
| GET | `/api/rag/stream/{agent_id}` | Yes | Stream indexing progress (SSE) |
| POST | `/api/rag/schema-cache/invalidate` | Yes | Clear schema cache (text-to-SQL) |
| POST | `/api/rag/webhook` | No | RAG completion webhook (from th2llm) |

### Webhooks

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/webhooks/subscriptions` | Yes | Create webhook subscription |
| GET | `/api/webhooks/subscriptions` | Yes | List subscriptions |
| PATCH | `/api/webhooks/subscriptions/{id}` | Yes | Update subscription |
| DELETE | `/api/webhooks/subscriptions/{id}` | Yes | Delete subscription |
| POST | `/api/webhooks/subscriptions/{id}/renew` | Yes | Manually renew subscription |
| POST | `/api/webhooks/gmail/notifications` | No | Gmail Pub/Sub push endpoint |
| POST | `/api/webhooks/outlook/notifications` | No | Outlook Graph push endpoint |
| GET | `/api/webhooks/logs` | Yes | List webhook execution logs |
| GET | `/api/webhooks/logs/{log_id}` | Yes | Get specific log |

### Integrations

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/integrations` | Yes | List user's connected integrations |
| GET | `/api/integrations/google/{service}/connect` | Yes | Get Google OAuth URL (drive, gmail, calendar, sheets, docs) |
| POST | `/api/integrations/google/callback` | Yes | Google OAuth callback |
| GET | `/api/integrations/microsoft/{service}/connect` | Yes | Get Microsoft OAuth URL (outlook, teams, onedrive, sharepoint) |
| POST | `/api/integrations/microsoft/callback` | Yes | Microsoft OAuth callback |
| GET | `/api/integrations/github/connect` | Yes | Get GitHub OAuth URL |
| POST | `/api/integrations/github/callback` | Yes | GitHub OAuth callback |
| DELETE | `/api/integrations/{provider}` | Yes | Disconnect integration |

### Notifications

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/notifications` | Yes | List notifications (paginated) |
| GET | `/api/notifications/unread-count` | Yes | Count unread notifications |
| GET | `/api/notifications/stream` | Yes | Stream notifications (SSE) |
| PATCH | `/api/notifications/{id}/read` | Yes | Mark as read |
| POST | `/api/notifications/read-all` | Yes | Mark all as read |

### Billing

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/billing/packages` | Yes | List credit packages |
| POST | `/api/billing/checkout` | Yes | Create Stripe checkout session |
| GET | `/api/billing/balance` | Yes | Get credit balance |
| GET | `/api/billing/transactions` | Yes | Transaction history (paginated) |
| GET | `/api/billing/portal` | Yes | Stripe customer portal URL |
| POST | `/api/billing/webhook` | No | Stripe webhook handler |

### Hub

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/hub` | Yes | List published agents |
| GET | `/api/hub/{hub_id}` | Yes | Get Hub agent details |
| POST | `/api/hub/publish` | Yes | Publish agent to Hub |
| POST | `/api/hub/clone` | Yes | Clone Hub agent |
| DELETE | `/api/hub/{hub_id}` | Yes | Remove from Hub |

### Files

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/files/upload` | Yes | Upload file |
| POST | `/api/files/upload-chunk` | Yes | Upload file chunk (resumable) |
| POST | `/api/files/upload-complete` | Yes | Mark chunked upload as complete |
| GET | `/api/files/{agent_id}/{filename}` | Yes | Download file |

### Artifacts

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/artifacts/{agent}/{user}/{session}` | Yes | List session artifacts |
| GET | `/api/artifacts/{agent}/{user}/{session}/{filename}` | Yes | Get artifact content |
| POST | `/api/artifacts/{agent}/{user}/{session}/{filename}/execute` | Yes | Execute artifact |

### API Keys

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/saved-api-keys` | Yes | List saved API keys |
| POST | `/api/saved-api-keys` | Yes | Save new API key |
| DELETE | `/api/saved-api-keys/{id}` | Yes | Delete API key |

### Data Lake

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/api/data-lake/pins/list` | Yes | List pins |
| POST | `/api/data-lake/pins/read` | Yes | Read pin data |
| POST | `/api/data-lake/pins/write` | Yes | Write pin data |

### Cloud File Browsers

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/googledrivebrowser/list` | Yes | List Google Drive files |
| GET | `/api/googledrivebrowser/search` | Yes | Search Google Drive |
| GET | `/api/googledrivebrowser/download` | Yes | Download file |
| GET | `/api/onedrivebrowser/list` | Yes | List OneDrive files |
| GET | `/api/onedrivebrowser/search` | Yes | Search OneDrive |
| GET | `/api/onedrivebrowser/download` | Yes | Download file |

### Scheduler (Mage)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/pipelines` | Yes | List Mage pipelines |
| POST | `/api/pipelines/agents/triggers` | Yes | Create trigger for agent |
| GET | `/api/pipelines/{uuid}/schedules` | Yes | List schedules |
| PUT | `/api/pipelines/schedules/{id}` | Yes | Update schedule |
| DELETE | `/api/pipelines/schedules/{id}` | Yes | Delete schedule |

### SuperAgents

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/superagents` | Yes | List SuperAgent templates |
| GET | `/api/superagents/{template_id}` | Yes | Get template details |

### Config

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/api/config` | No | Public app configuration |

---

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

th2agent supports two categories of OAuth:

1. **User Login** — Sign up / log in via OAuth (GitHub, Google, Microsoft, LinkedIn)
2. **Workspace Integrations** — Connect external services as tools for agents

#### Connecting a Workspace Integration

1. User calls `GET /api/integrations/{provider}/{service}/connect` to get the OAuth URL
2. User authorizes the app on the provider's consent screen
3. Provider redirects to `POST /api/integrations/{provider}/callback`
4. th2agent stores the access/refresh tokens encrypted in the `integrations` table
5. Agent tools can now use the integration tokens

#### Supported Services

| Provider | Services | Scopes |
|----------|----------|--------|
| **Google** | Drive, Gmail, Calendar, Sheets, Docs | Service-specific (e.g., `gmail.readonly`, `drive.readonly`) |
| **Microsoft** | Outlook, Teams, OneDrive, SharePoint | Service-specific (e.g., `Mail.Read`, `Files.ReadWrite`) |
| **GitHub** | Repositories, Gists | `repo`, `gist`, `read:user` |

---

## Webhooks

th2agent can automatically trigger agents when events occur in connected services (new email, etc.). Two providers are supported: **Gmail** (via Google Pub/Sub) and **Outlook** (via Microsoft Graph subscriptions).

### How It Works

```
Email arrives → Provider pushes notification → th2agent receives it
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
Gmail  ──push──▶  Google Pub/Sub  ──HTTP POST──▶  th2agent
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
- A **publicly accessible URL** for th2agent

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

In the th2agent-app UI: **Integrations → Connect Google (Gmail)**

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

## Billing (Stripe)

### Credit Packages

| Package | Credits | Description |
|---------|---------|-------------|
| Starter | 100 | — |
| Pro | 500 | — |
| Business | 1,000 | — |
| Enterprise | 5,000 | — |

### Flow

1. `GET /api/billing/packages` — List available packages
2. `POST /api/billing/checkout` — Create Stripe checkout session
3. User completes payment on Stripe
4. Stripe webhook (`checkout.session.completed`) credits the user's balance
5. `GET /api/billing/balance` — Check updated balance

### Transaction Types

| Type | Description |
|------|-------------|
| `purchase` | Credit purchase via Stripe |
| `usage` | Credit deduction from agent runs |
| `refund` | Stripe refund |
| `bonus` | Admin-granted bonus credits |

---

## Scheduler

Agent runs can be automated on a schedule via Mage AI:

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
| `user` | User accounts | email, password, credits, stripe_customer_id, OAuth IDs, MFA config, role |
| `integrations` | Connected OAuth services | user_id, provider, access_token, refresh_token, scopes, meta |
| `webhook_subscriptions` | Active webhook watches | user_id, integration_id, provider, subscription_id, resource, agent_id, status, expiration |
| `webhook_logs` | Webhook execution history | subscription_id, trigger_event, email_subject, agent_response, duration_ms, status |
| `notifications` | User notifications | user_id, title, message, type (webhook/info/warning/error), is_read |
| `transactions` | Credit movements | user_id, type (purchase/usage/refund/bonus), amount, balance_after, stripe IDs |
| `credit_purchases` | Stripe purchase sessions | user_id, stripe_session_id, credits_amount, price_paid, status |

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

See [`readme/README.md`](./readme/README.md) for a step-by-step API guide with screenshots and detailed examples.

---

## Support

For questions or contributions, contact the thaink² team.

## License

Proprietary - thaink² 2025
