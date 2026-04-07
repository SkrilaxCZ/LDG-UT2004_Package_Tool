ENV := uv run

.PHONY: install
install:
	@echo "Syncing dependencies (incl. dev extras)..."
	uv sync --extra dev
	@echo "Environment ready. Use 'uv run <cmd>' to run inside the venv."

.PHONY: format
format:
	$(ENV) ruff format .
	$(ENV) ruff check --select I --fix .

.PHONY: check
check:
	$(ENV) ruff check --select I .
	$(ENV) mypy ut2004packageutil

.PHONY: hooks
hooks:
	$(ENV) pre-commit install

.PHONY: pre-commit
pre-commit:
	$(ENV) pre-commit run --all-files
