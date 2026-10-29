PY := .venv/bin/python
PORT ?= 8000
# The first Python on PATH that is 3.12 or newer (macOS ships 3.9 as python3).
PYTHON ?= $(shell for p in python3.14 python3.13 python3.12 python3; do \
	command -v $$p >/dev/null 2>&1 && $$p -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null && { echo $$p; break; }; done)

.PHONY: setup run test coverage lint live docker

setup:  ## venv, dependencies, .env, database (SQLite) loaded from the committed data
	@if [ -x $(PY) ] && ! $(PY) -c 'import sys; sys.exit(sys.version_info < (3, 12))' 2>/dev/null; then \
		echo "Removing .venv: it was built with Python older than 3.12"; rm -rf .venv; fi
	@if [ ! -x $(PY) ]; then \
		if command -v uv >/dev/null 2>&1; then uv venv --python 3.14 .venv; \
		elif [ -n "$(PYTHON)" ]; then echo "Using $(PYTHON)"; $(PYTHON) -m venv .venv; \
		else echo "Python 3.12+ not found. Install uv (https://docs.astral.sh/uv/) or Python 3.12+ (python.org), then run make setup again."; exit 1; fi; fi
	@if command -v uv >/dev/null 2>&1; then uv pip install -q --python $(PY) -r requirements-dev.txt; \
		else $(PY) -m pip install -q -r requirements-dev.txt; fi
	@test -f .env || cp .env.example .env
	$(PY) manage.py migrate
	@echo; echo "Ready. Start the server with 'make run', then open http://localhost:$(PORT)/map"

run:
	@test -x $(PY) || { echo "Run 'make setup' first."; exit 1; }
	$(PY) manage.py runserver $(PORT)

test:
	$(PY) -m pytest

coverage:  ## tests with a coverage report (fails under the minimum in pyproject.toml)
	$(PY) -m coverage run -m pytest && $(PY) -m coverage report

lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check .

live:  ## tests that call the real routing API
	RUN_LIVE=1 $(PY) -m pytest -m live

docker:
	docker compose up --build
