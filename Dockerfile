FROM python:3.13-slim AS runtime

ARG APPOWERB_VERSION
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        unixodbc-dev \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip \
    && pip install --no-cache-dir "apowerb==${APPOWERB_VERSION}"

EXPOSE 8000

CMD ["apowerb", "serve", "--host", "0.0.0.0", "--port", "8000", "--no-reload"]
