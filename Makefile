.PHONY: install test lint fmt demo run api recon recon-eval stress invariants docker up down clean

install:
	uv sync --python 3.12

test:
	uv run pytest -q

lint:
	uvx ruff check .

fmt:
	uvx ruff check --fix .

demo:
	uv run ledgercore demo --fresh

run api: demo
	uv run ledgercore api --port 8000

recon:
	uv run ledgercore recon --day 2026-11-04

recon-eval:
	uv run ledgercore recon-eval

stress:
	uv run ledgercore stress --threads 16 --ops 100

invariants:
	uv run ledgercore invariants

docker:
	docker build -t ledgercore:local .

up:
	docker compose up --build -d

down:
	docker compose down

clean:
	rm -rf data .pytest_cache .ruff_cache .hypothesis
