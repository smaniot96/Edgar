# Run from the Edgar directory: `make up`, `make ingest PDF=phb.pdf`, etc.
#
# `docker compose up -d` only starts the runtime stack (api, db, redis, qdrant). `migrate` and
# `ingestion` sit behind the `tools` compose profile and `rules_engine` behind `rules`;
# `docker compose run` enables a service's profile automatically, so the targets below work.
.PHONY: up down logs migrate seed ingest rules test lint check-migrations wait-api \
	frontend-install frontend-dev frontend-build

COMPOSE ?= docker compose
PDF ?=
COLLECTION ?= rules_player_handbook
API_URL ?= http://localhost:8000
WAIT_TIMEOUT_SEC ?= 60

up: wait-api
	curl -fsS -X POST $(API_URL)/api/seed | jq

wait-api: migrate
	$(COMPOSE) up -d
	@echo "Waiting up to $(WAIT_TIMEOUT_SEC)s for $(API_URL)/health ..."
	@for i in $$(seq 1 $(WAIT_TIMEOUT_SEC)); do \
		if curl -fsS $(API_URL)/health >/dev/null 2>&1; then \
			echo "API ready after $${i}s"; exit 0; \
		fi; \
		sleep 1; \
	done; \
	echo "API not ready after $(WAIT_TIMEOUT_SEC)s; try \`make logs\`"; exit 1

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f api

# Starts db (dependency) if needed, then applies Alembic migrations.
migrate:
	$(COMPOSE) run --rm migrate

seed:
	curl -fsS -X POST $(API_URL)/api/seed | jq

ingest:
	$(COMPOSE) run --rm ingestion /data/$(PDF) --collection $(COLLECTION)

rules:
	$(COMPOSE) run --rm rules_engine

test:
	uv run pytest tests/

lint:
	uv run ruff check .

# Fails if the ORM models and the migrations disagree (needs a migrated database; uses .env).
check-migrations:
	cd db && uv run --project .. python -m alembic check

frontend-install:
	cd frontend && npm install

frontend-dev:
	cd frontend && npm run dev

frontend-build:
	cd frontend && npm run build
