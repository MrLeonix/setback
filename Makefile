.PHONY: dev test lint typecheck fixtures run-local deploy demo-reset

dev:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .

typecheck:
	uv run mypy

fixtures:
	uv run python tools/fetch_fixtures.py

run-local:
	uv run uvicorn setback.console.app:app --reload --port 8000

deploy:
	./deploy.sh

demo-reset:
	@echo "Not yet implemented: reset Firestore demo-case documents once state/firestore.py lands"
	@exit 1
