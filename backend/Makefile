# LabForge backend — common tasks
VENV ?= /tmp/labforge-venv
PY := $(VENV)/bin/python

.PHONY: help install-dev test test-cov run

help:
	@echo "install-dev  install test tooling into $(VENV)"
	@echo "test         run the test suite"
	@echo "test-cov     run tests with coverage"
	@echo "run          start the dev server on :8899"

install-dev:
	$(VENV)/bin/pip install -r requirements-dev.txt

test:
	$(PY) -m pytest

test-cov:
	$(PY) -m pytest --cov=app --cov-report=term-missing

run:
	$(VENV)/bin/uvicorn app.main:app --host 0.0.0.0 --port 8899
