setup:
    uv sync --group dev
    uv run pre-commit install

lint:
    uv run pre-commit run --all-files

test:
    DB_SCHEMA="" uv run pytest -q -m "not integration"
