FROM python:3.13-slim AS runtime

ARG APPOWERB_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        unixodbc-dev \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./

RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && /root/.local/bin/uv sync --no-dev --no-install-project \
    && /root/.local/bin/uv pip install --system --no-cache-dir "apowerb==${APPOWERB_VERSION}"

EXPOSE 8000

CMD ["apowerb", "serve", "--host", "0.0.0.0", "--port", "8000", "--no-reload"]
