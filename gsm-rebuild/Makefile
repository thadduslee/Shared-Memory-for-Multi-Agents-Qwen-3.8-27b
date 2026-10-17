.PHONY: help install db db-docker db-stop migrate test test-unit lint format typecheck check demo serve seed

PY ?= python
export GSM_DATABASE_URL ?= postgresql+psycopg://gsm_owner@localhost:55432/gsm
export GSM_TEST_DATABASE_URL ?= postgresql+psycopg://gsm_owner@localhost:55432/gsm_test

help:
	@echo "install     pip install -e .[dev]"
	@echo "db          start a local PostgreSQL 16 + pgvector without Docker (scripts/local_pg.sh)"
	@echo "db-docker   start PostgreSQL + pgvector with docker compose"
	@echo "migrate     alembic upgrade head"
	@echo "test        all tests (WIPES \$$GSM_TEST_DATABASE_URL)"
	@echo "test-unit   unit tests only (no database)"
	@echo "lint        ruff check + format check"
	@echo "typecheck   mypy --strict"
	@echo "check       lint + typecheck + test"
	@echo "demo        run the acceptance scenarios against \$$GSM_DATABASE_URL"
	@echo "serve       run the API on :8080"

install:
	$(PY) -m pip install -e ".[dev]"

db:
	scripts/local_pg.sh start

db-docker:
	docker compose up -d db

db-stop:
	scripts/local_pg.sh stop

migrate:
	gsm migrate

test:
	$(PY) -m pytest

test-unit:
	GSM_TEST_DATABASE_URL= $(PY) -m pytest tests/unit

lint:
	ruff check src tests migrations
	ruff format --check src tests migrations

format:
	ruff format src tests migrations
	ruff check --fix src tests migrations

typecheck:
	mypy

check: lint typecheck test

demo:
	gsm demo

seed:
	gsm seed --tenant acme

serve:
	gsm serve --host 0.0.0.0 --port 8080
