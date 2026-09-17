<p align="center">
  <img src="https://docs.apowerb.com/logo/apowerb-wide.png" alt="apowerb" height="80"/>
</p>

<p align="center">
  <strong>The open-source agentic framework to build, orchestrate, and operate production AI agents.</strong>
</p>

<p align="center">
  <a href="https://docs.apowerb.com/">Documentation</a> •
  <a href="https://docs.apowerb.com/quickstart">Quickstart</a> •
  <a href="https://github.com/apowerb/apowerb">GitHub</a> •
  <a href="https://thaink2.com">thaink2</a>
</p>

---

## What is apowerb?

apowerb is an agentic framework built on **FastAPI** and **Google ADK**. It gives you
the pieces you need to run agents in production rather than in a notebook:
orchestration, a tool store, RAG, Text-to-SQL, webhooks and observability.

It is Apache-2.0 licensed and runs anywhere Docker runs — no managed service required.

## Quick start

```bash
docker run -d --name apowerb \
  -p 8000:8000 \
  -e GOOGLE_API_KEY=your-key \
  apowerb/apowerb:latest
```

The API is then available on `http://localhost:8000`, and the interactive
documentation on `http://localhost:8000/docs`.

For a full deployment — database, UI, RAG and workers — use the Compose stack
described in the [deployment guide](https://docs.apowerb.com/deployment/dockercompose).

## Tags

| Tag | Content |
|-----|---------|
| `latest` | Latest published release |
| `x.y.z` | A specific release |

## Configuration

apowerb is configured through environment variables. The
[configuration reference](https://docs.apowerb.com/configuration) lists them all;
the essentials are a model provider key and a database URL.

## Ecosystem

| Image | Role |
|-------|------|
| `apowerb/apowerb` | The core agentic API |
| `apowerb/apowerb-ui` | The web interface |
| `apowerb/th2etl` | ETL and automation |
| `apowerb/th2pulse` | OpenTelemetry observability |

## License

Apache-2.0. Source code and issue tracker on
[GitHub](https://github.com/apowerb/apowerb).
