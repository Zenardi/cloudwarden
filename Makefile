.DEFAULT_GOAL := help
COMPOSE := docker compose

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install backend runtime deps
	pip install -r backend/requirements.txt

install-dev: ## Install backend + test/dev deps
	pip install -r backend/requirements-dev.txt

lint: ## Ruff lint
	ruff check backend

fmt: ## Ruff format
	ruff format backend

test: ## Run the offline unit tests
	pytest

coverage: ## Run the full suite with the 95% gate (needs Docker for integration tests)
	pytest backend/tests --cov=cloudwarden --cov-report=term-missing

run-mock: ## Run the full pipeline against fixtures (no Azure), local
	cd backend && FINOPS_MOCK=1 DATABASE_URL=$${DATABASE_URL:-postgresql+psycopg://finops:finops@localhost:5432/finops} python -m cloudwarden.cli run --mock

up: ## Start the full stack (db + backend + grafana + frontend)
	$(COMPOSE) up -d --build

up-core: ## Start without the frontend (db + backend + grafana only)
	$(COMPOSE) up -d --build db backend grafana

up-all: ## Alias for `up` (frontend is part of the default stack)
	$(COMPOSE) up -d --build

down: ## Stop the stack
	$(COMPOSE) down

logs: ## Tail stack logs
	$(COMPOSE) logs -f

initdb: ## Create/upgrade the database schema (in-container)
	$(COMPOSE) run --rm backend initdb

seed: ## Run one mock pipeline inside the backend container
	$(COMPOSE) run --rm backend run --mock

.PHONY: help install install-dev lint fmt test coverage run-mock up up-core up-all down logs initdb seed
