.PHONY: install up down lint format typecheck test test-integration ci trec-download trec-load ingest-trec ingest-sample psql

install:
	pip install -e ".[dev]"

up:
	docker compose up -d db

down:
	docker compose down

lint:
	ruff check src tests
	ruff format --check src tests

format:
	ruff format src tests
	ruff check --fix src tests

typecheck:
	mypy

test:
	pytest --cov=ctmatch --cov-report=term-missing

test-integration:
	pytest -m integration

ci: lint typecheck test

trec-download:
	bash scripts/download_trec.sh data/trec

trec-load:
	python -m ctmatch.flows.load_trec --data-dir data/trec

ingest-trec:
	python -m ctmatch.flows.ingest_trials --trec-judged

ingest-sample:
	python -m ctmatch.flows.ingest_trials --query "type 2 diabetes" --max-pages 2

psql:
	docker compose exec db psql -U ctmatch -d ctmatch
