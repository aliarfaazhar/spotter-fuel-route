PY := .venv/bin/python

.PHONY: setup run test lint docker live
setup:  ## create the venv, install deps, build the DB (SQLite) from the committed data
	uv venv --python 3.12 .venv 2>/dev/null || python3.12 -m venv .venv
	uv pip install --python $(PY) -r requirements-dev.txt 2>/dev/null || $(PY) -m pip install -r requirements-dev.txt
	$(PY) manage.py migrate

run:
	$(PY) manage.py runserver 8000

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check .

live:  ## tests that call the real routing API
	RUN_LIVE=1 $(PY) -m pytest -m live

docker:
	docker compose up --build
