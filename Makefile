PY := .venv/bin/python

.PHONY: setup run test lint live docker

setup:  ## Python >= 3.12: venv, dependencies, .env, database (SQLite) loaded from the committed data
	@command -v uv >/dev/null && uv venv --python 3.12 .venv || python3 -m venv .venv
	@$(PY) -c 'import sys; assert sys.version_info >= (3, 12), "Python 3.12+ is required (Django 6.1)"'
	@command -v uv >/dev/null && uv pip install --python $(PY) -r requirements-dev.txt || $(PY) -m pip install -r requirements-dev.txt
	@test -f .env || cp .env.example .env
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
