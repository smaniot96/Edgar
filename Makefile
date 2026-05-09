# Run from the Edgar directory: `make up`, `make ingest PDF=phb.pdf`, etc.
.PHONY: up down logs migrate seed ingest test

COMPOSE ?= docker compose
PDF ?=
COLLECTION ?= rules_player_handbook

up:
	$(COMPOSE) up -d
	$(COMPOSE) run --rm migrate
	curl -fsS -X POST http://localhost:8000/api/seed | jq

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f api

migrate:
	$(COMPOSE) run --rm migrate

seed:
	curl -fsS -X POST http://localhost:8000/api/seed | jq

ingest:
	$(COMPOSE) run --rm ingestion python -m ingestion.main /data/$(PDF) --collection $(COLLECTION)

test:
	uv run pytest tests/
