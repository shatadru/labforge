# LabForge top-level tasks.
# Version resolution lives in scripts/version.sh: a release tag (vX.Y.Z), a CI
# build (X.Y.Z-ci.<sha>) or a local dev build (X.Y.Z-dev). Bump the base with
# `make bump-patch|bump-minor|bump-major` (edits the top-level VERSION file).
VERSION ?= $(shell ./scripts/version.sh 2>/dev/null || echo 0.1.0)
PYTHON  ?= python3
# Prefer a system nfpm, fall back to the vendored binary if present.
NFPM    ?= $(shell command -v nfpm 2>/dev/null || echo ./bin/nfpm)
IMAGE   ?= labforge/control:$(VERSION)
# Constrain nfpm virtual memory to <=1 GB (address space) to avoid system OOM.
NPM_MEM_LIMIT := 1048576

.PHONY: help test run dev install package rpm deb docker docker-push version \
        bump-patch bump-minor bump-major clean clean-pyc

help:
	@echo "test        run the backend test suite (tox)"
	@echo "run         start the backend for local development"
	@echo "dev         run control + in-process agent with LOCAL_AGENT=true"
	@echo "install     one-command local systemd install (deploy/install-local.sh)"
	@echo "package     build the agent .deb and .rpm with nfpm"
	@echo "docker      build the control-plane container image"
	@echo "version     print the resolved build version"
	@echo "bump-patch  advance the VERSION file (also bump-minor, bump-major)"
	@echo "clean       remove build output"

version:
	@./scripts/version.sh

bump-patch:
	@./scripts/bump-version.sh patch

bump-minor:
	@./scripts/bump-version.sh minor

bump-major:
	@./scripts/bump-version.sh major

test:
	$(MAKE) -C backend test

run:
	$(MAKE) -C backend run

dev:
	cd backend && MODE=control LOCAL_AGENT=true \
		TEMPLATES_DIR=$$(pwd)/cloud_init_templates \
		$${VENV:-.venv}/bin/uvicorn app.main:app --reload --port 8899

install:
	./deploy/install-local.sh "$(CURDIR)"

clean-pyc:
	find backend/app -name '__pycache__' -type d -prune -exec rm -rf {} +

package: clean-pyc
	mkdir -p dist
	(ulimit -v $(NPM_MEM_LIMIT); VERSION=$(VERSION) $(NFPM) pkg --config deploy/nfpm.yaml --packager deb \
		--target dist/labforge-agent_$(VERSION)_amd64.deb)
	(ulimit -v $(NPM_MEM_LIMIT); VERSION=$(VERSION) $(NFPM) pkg --config deploy/nfpm.yaml --packager rpm \
		--target dist/labforge-agent-$(VERSION)-1.x86_64.rpm)
	@echo "Built:"; ls -1 dist/*.deb dist/*.rpm 2>/dev/null || true

rpm: clean-pyc
	mkdir -p dist
	(ulimit -v $(NPM_MEM_LIMIT); VERSION=$(VERSION) $(NFPM) pkg --config deploy/nfpm.yaml --packager rpm \
		--target dist/labforge-agent-$(VERSION)-1.x86_64.rpm)

deb: clean-pyc
	mkdir -p dist
	(ulimit -v $(NPM_MEM_LIMIT); VERSION=$(VERSION) $(NFPM) pkg --config deploy/nfpm.yaml --packager deb \
		--target dist/labforge-agent_$(VERSION)_amd64.deb)

docker:
	docker build --build-arg APP_VERSION=$(VERSION) -t $(IMAGE) backend/

docker-push:
	docker push $(IMAGE)

clean:
	rm -rf dist deploy/dist .pytest_cache backend/.pytest_cache backend/.tox
	find backend -name '__pycache__' -type d -prune -exec rm -rf {} +
