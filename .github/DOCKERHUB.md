<p align="center">
  <img src="https://docs.apowerb.com/logo/apowerb-wide.png" alt="apowerb" height="80"/>
</p>

<p align="center">
  <a href="https://docs.apowerb.com/">Documentation</a> •
  <a href="https://docs.apowerb.com/quickstart">Quickstart</a> •
  <a href="https://github.com/apowerb/apowerb">GitHub</a> •
  <a href="https://thaink2.com">thaink2</a>
</p>

---

**apowerb** is an open-source agentic framework for building, running, and operating AI agents at scale. It provides a full-stack runtime environment featuring a FastAPI backend, Typer CLI, and REST API designed to pair seamlessly with frontend interfaces like `apowerb-ui`.

Agents are stored in the database, materialized dynamically as Python modules at startup, and executed through Google ADK (Agent Development Kit). Model connectivity is managed via LiteLLM, offering plug-and-play support for any major LLM provider without modifying agent code.

---

## 🚀 Key Features

* **Dynamic Agent Materialization:** Agent definitions stored in the database are converted into runnable Python modules on boot.
* **Google ADK Core:** High-performance agent execution powered by Google's Agent Development Kit framework.
* **Universal Model Support:** Powered by LiteLLM—connect OpenAI, Anthropic, Gemini, local Ollama models, or custom endpoints via unified configuration.
* **Advanced Capability Stack:** Built-in support for Retrieval-Augmented Generation (RAG), Text-to-SQL querying, custom tool integrations, webhooks, and billing management.
* **Multi-Interface Support:** Control agents via FastAPI REST endpoints, the Typer CLI, or direct API clients.

---

## 🏃 Quick Start

### 1. Direct Docker Usage (`docker run`)

To run the container directly, ensure you have a running PostgreSQL database accessible to the container, along with your required environment secrets:

```bash
docker run -d \
  --name apowerb \
  -p 8000:8000 \
  -e DB_NAME=th2agent \
  -e DB_USER=th2agent \
  -e DB_PASSWORD=your_secure_password \
  -e ENCRYPT_KEY="your_32_byte_base64_fernet_key" \
  -e TEST_TOKEN="your_test_token" \
  -e DEFAULT_LLM_MODEL="gemini/gemini-2.5-flash" \
  -e DEFAULT_LLM_API_KEY="your_api_key" \
  apowerb/apowerb:latest
```

### 2. Docker Compose Deployment

The recommended way to deploy `apowerb` along with PostgreSQL and the companion frontend is using the setup provided in the [apowerb-hosting repository](https://github.com/apowerb/apowerb-hosting/tree/main/docker-compose).

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/apowerb/apowerb-hosting.git](https://github.com/apowerb/apowerb-hosting.git)
   cd apowerb-hosting/docker-compose
   ```

2. **Set up environment variables:**
   Copy the sample environment file and generate secret keys:
   ```bash
   cp ../.env.example .env
   ./scripts/generate-secrets.sh
   ```

3. **Start the services:**
   ```bash
   docker compose up -d
   ```

---

## ⚙️ Environment Variables

Configure your container or `.env` file using the options provided in [.env.example](https://github.com/apowerb/apowerb-hosting/blob/main/.env.example):

### Required Setup
| Variable | Description | Required | Default |
| :--- | :--- | :--- | :--- |
| `ENCRYPT_KEY` | Valid Fernet key (32 bytes base64) used to encrypt integration tokens | **Yes** | — |
| `TEST_TOKEN` | Authentication token required at startup | **Yes** | — |
| `DB_NAME` | PostgreSQL database name | **Yes** | `th2agent` |
| `DB_USER` | PostgreSQL database user | **Yes** | `th2agent` |
| `DB_PASSWORD` | PostgreSQL database password | **Yes** | `change-me` |

### Network & Port Configuration
| Variable | Description | Default |
| :--- | :--- | :--- |
| `TH2AGENT_PORT` | Backend API port | `8000` |
| `FRONT_PORT` | Frontend UI port | `3000` |
| `PUBLIC_API_URL` | Public base URL accessible by the client browser | `http://localhost:8000` |
| `APP_HOST` | Domain served when using reverse proxies like Traefik | `app.example.com` |
| `ACME_EMAIL` | Email address for TLS certificate issuance | `admin@example.com` |
| `DB_SSLMODE` | SSL mode for Postgres connection (`disable` / `require`) | `disable` |

### Container Images
| Variable | Description | Default |
| :--- | :--- | :--- |
| `APOWERB_BACKEND_IMAGE` | Docker image for the backend service | `apowerb/apowerb` |
| `APOWERB_BACKEND_TAG` | Docker image tag for the backend | `latest` |
| `APOWERB_FRONTEND_IMAGE` | Docker image for the frontend UI | `apowerb/apowerb-ui` |
| `APOWERB_FRONTEND_TAG` | Docker image tag for the frontend UI | `latest` |

### Shared Model Defaults (LiteLLM)
| Variable | Description | Default |
| :--- | :--- | :--- |
| `DEFAULT_LLM_MODEL` | Default shared LLM ID using LiteLLM format (e.g. `gemini/gemini-2.5-flash`) | — |
| `DEFAULT_LLM_API_KEY` | API key for the default shared model provider | — |
| `DEFAULT_LLM_API_BASE` | Optional custom base URL endpoint for LiteLLM | — |

### System Mailer & Configuration
| Variable | Description | Default |
| :--- | :--- | :--- |
| `NOTIFICATION_EMAIL` | Mailbox address for system notifications | — |
| `NOTIFICATION_INTEGRATION_OWNER` | DB user account owning the notification integration | — |
| `SUPER_ADMIN_EMAIL` | Email recipient for ETL and critical alerts | — |
| `TH2_EXTENSIONS` | Comma-separated list of commercial extension modules | — |
| `WORKING_MODE` | Application execution environment | `development` |

---

## 📚 Documentation & Resources

* **Documentation:** [docs.apowerb.com](https://docs.apowerb.com/)
* **Hosting Repo:** [apowerb-hosting](https://github.com/apowerb/apowerb-hosting)

---

## 🛰️ API Access & Docs

Once running, access the interactive API documentation and health checks:

* **Swagger UI:** `http://localhost:8000/docs`
* **ReDoc:** `http://localhost:8000/redoc`
* **Health Check:** `http://localhost:8000/health`
