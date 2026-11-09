.PHONY: install lint type test check env up down migrate test-schema test-integration env-fill seed dev-tokens api smoke demo anchor-backfill bench-data bench-estimate bench

install:
	pip install -e ".[dev]"

lint:
	ruff check src tests bench

type:
	mypy

test:
	pytest --cov=gawe --cov-report=term-missing

check: lint type test

# Creates .env with generated secrets if it doesn't exist; never overwrites an existing one.
env:
	@if [ -f .env ]; then echo ".env already exists, leaving it alone"; else \
	  cp .env.example .env && \
	  sed -i.bak "s|^GAWE_DB_PASSWORD=.*|GAWE_DB_PASSWORD=$$(python3 -c 'import secrets; print(secrets.token_hex(16))')|" .env && \
	  sed -i.bak "s|^GAWE_POLICY_SIGNING_KEY=.*|GAWE_POLICY_SIGNING_KEY=$$(python3 -c 'import secrets; print(secrets.token_hex(32))')|" .env && \
	  rm -f .env.bak && echo "created .env with generated secrets"; fi

up: env
	docker compose up -d

down:
	docker compose down

# Apply Alembic migrations to the compose database (reads .env).
migrate:
	@set -a; . ./.env; set +a; alembic upgrade head

# SQL-level schema tests: builds a throwaway database inside the compose Postgres.
test-schema:
	@set -a; . ./.env; set +a; \
	PSQL="docker compose exec -T postgres psql -U $$GAWE_DB_USER" bash scripts/test_schema.sh

# Python tests that need a live Postgres (isolated schema, dropped afterwards).
test-integration:
	@set -a; . ./.env; set +a; \
	GAWE_TEST_DATABASE_URL="postgresql+psycopg://$$GAWE_DB_USER:$$GAWE_DB_PASSWORD@localhost:5432/$$GAWE_DB_NAME" \
	pytest tests/integration tests/contract -v


# Adds demo settings missing from an existing .env. Never changes a line that is already there.
env-fill:
	@test -f .env || { echo ".env missing: run 'make env' first"; exit 1; }
	@add() { grep -q "^$$1=" .env || { echo "$$1=$$2" >> .env; echo "added $$1"; }; }; \
	add GAWE_POLICY_SIGNING_KEY "$$(python3 -c 'import secrets; print(secrets.token_hex(32))')"; \
	add GAWE_APP "gawe.apps.contract_review:bundle"; \
	add GAWE_API_TOKENS_FILE ".dev-tokens.json"; \
	add GAWE_STORE "postgres"; \
	grep -q "^GAWE_LLM_MODEL=." .env || echo "NOTE: set GAWE_LLM_MODEL and ANTHROPIC_API_KEY in .env"

# Demo users (alice, bob, fiona, root) in the compose database.
seed:
	@set -a; . ./.env; set +a; \
	docker compose exec -T postgres psql -U $$GAWE_DB_USER -d $$GAWE_DB_NAME \
	  -v ON_ERROR_STOP=1 -q < scripts/seed_dev.sql && echo "seeded"

# Writes .dev-tokens.json and prints one token per user. Keep the output.
dev-tokens:
	python scripts/gen_dev_tokens.py

# Runs the API on http://127.0.0.1:8000 (docs at /docs).
api:
	@set -a; . ./.env; set +a; \
	uvicorn --factory gawe.api.main:create_app_from_env --host 127.0.0.1 --port 8000

# Tries the real model on the sample documents with no side effects: make smoke APP=contract_review
APP ?= contract_review
smoke:
	@set -a; . ./.env; set +a; python scripts/live_smoke.py $(APP)

# First-time setup in one go, then start the API.
demo: env env-fill up
	@sleep 3
	$(MAKE) migrate seed dev-tokens
	$(MAKE) api

ui-install:
	cd ui && npm install

ui-dev:
	cd ui && npm run dev

ui-check:
	cd ui && npm run check

app-role:  ## create the limited DB role the API runs as (after `make up migrate`)
	@set -a; . ./.env; set +a; bash scripts/create_app_role.sh

check-docs:  ## every threat-model row must name an existing test
	python scripts/check_threat_model.py

anchor-backfill:  ## anchor finished runs that have no anchor yet (needs GAWE_ANCHOR_SINKS)
	@set -a; . ./.env; set +a; python scripts/anchor_backfill.py

# Benchmark (M12). bench-data downloads CUAD (~106 MB) into .bench-data/ and checks its pinned hash.
bench-data:
	python -m bench data

# Both need a spend cap and the model settings in .env, e.g. make bench MAX_USD=25
bench-estimate:  ## price a benchmark run (free token count; --trial costs money)
	@set -a; . ./.env; set +a; python -m bench estimate --max-usd $(MAX_USD)

bench:  ## run the benchmark under a spend cap
	@set -a; . ./.env; set +a; python -m bench run --max-usd $(MAX_USD)
