# ForgeOps root Makefile — Phase 0 contracts (design.md §13.4).
#
# Requires GNU make and a POSIX shell (Git Bash or WSL2 on Windows).
# Business logic lives in scripts/*.sh so the same commands work everywhere.

SHELL := /bin/sh
.DEFAULT_GOAL := help

# Pinned toolchain versions verified by bootstrap (design.md §16.2, §16.4).
DOCKER_COMPOSE_VERSION := 2.24.7
PIP_TOOLS_VERSION := 7.6.0

.PHONY: help bootstrap init-env init-ca clean
.PHONY: build build-agent build-backend build-frontend
.PHONY: test test-agent test-backend test-frontend
.PHONY: lint lint-agent lint-backend lint-frontend lint-chokepoint

help: ## List the available targets
	@printf 'ForgeOps — Phase 0 make targets\n\n'
	@awk 'BEGIN { FS = ":[^#]*## " } /^[a-zA-Z0-9_.-]+:[^#]*## / { printf "  %-14s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf '\nRequires GNU make and a POSIX shell. See docs/development.md.\n'

# ─── Build targets ──────────────────────────────────────────────────────────
build: build-agent build-backend build-frontend ## Build all components

build-agent: ## Build the Go agent
	@printf '==> build-agent: go build ./...\n'
	@cd agent && go build ./...

build-backend: ## Build backend Docker image
	@printf '==> build-backend: docker build\n'
	@docker build -t forgeops-backend:dev backend/

# ─── Test targets ───────────────────────────────────────────────────────────
test: test-agent test-backend test-frontend ## Run all test suites

test-agent: ## Run Go agent tests with the race detector (PRD §9)
	@printf '==> test-agent: go test -race -shuffle=on ./... -count=1\n'
	@cd agent && CGO_ENABLED=1 go test -race -shuffle=on ./... -count=1

test-backend: ## Run Python backend tests
	@printf '==> test-backend: pytest tests/ -q\n'
	@# The preflight REFUSES a run whose backing services are down, rather than letting it proceed.
	@# Without it the DB-backed property tests skip, the mutation harness then correctly reports that
	@# Q-04/Q-16/Q-17 executed no tests and Q-05 passed under its own negative control -- and all of
	@# that arrives 66 minutes in, under a summary line that reads like success. It does not start
	@# anything: a test command that mutated the developer's environment would produce results that
	@# depend on state they did not choose. See scripts/check-test-services.sh.
	@bash scripts/check-test-services.sh
	@cd backend && .venv/Scripts/python -m pytest tests/ -q

# ─── Lint targets ───────────────────────────────────────────────────────────
lint: lint-agent lint-backend lint-frontend lint-chokepoint ## Lint all components

lint-chokepoint: ## Assert the mutation chokepoint is unbypassable (design §2.2.1, Q-03)
	@printf '==> lint-chokepoint: reachability over both runtimes\n'
	@bash scripts/check-chokepoint.sh

lint-agent: ## Lint Go agent with golangci-lint
	@printf '==> lint-agent: golangci-lint v1.62.2 from agent/tools (checksum-verified)\n'
	@cd agent && bash ../scripts/go-tool.sh github.com/golangci/golangci-lint/cmd/golangci-lint run ./...

lint-backend: ## Lint Python backend with ruff
	@printf '==> lint-backend: ruff check\n'
	@cd backend && ruff check src/ tests/

bootstrap: ## Verify the pinned toolchain; never rewrites lockfiles
	@printf '==> bootstrap: verifying pinned toolchain (Docker Compose %s, pip-tools==%s)\n' '$(DOCKER_COMPOSE_VERSION)' '$(PIP_TOOLS_VERSION)'
	@FORGEOPS_DOCKER_COMPOSE_VERSION='$(DOCKER_COMPOSE_VERSION)' \
		FORGEOPS_PIP_TOOLS_VERSION='$(PIP_TOOLS_VERSION)' \
		sh scripts/bootstrap.sh

init-env: ## Create .env from .env.example only when .env is absent
	@printf '==> init-env: ensuring a local .env exists without overwriting one\n'
	@sh scripts/init-env.sh

init-ca: ## Generate a development internal CA into .env only when absent; never overwrites
	@printf '==> init-ca: ensuring a development internal CA exists without overwriting one\n'
	@sh scripts/init-ca.sh

clean: ## Remove build output only; never .env, lockfiles or Docker volumes
	@printf '==> clean: removing build output (preserving .env, lockfiles and Docker volumes)\n'
	@rm -rf dist agent/dist frontend/.next frontend/out frontend/coverage
	@rm -rf backend/htmlcov backend/coverage.xml backend/.coverage backend/.pytest_cache
	@rm -rf .pytest_cache .ruff_cache backend/.ruff_cache agent/coverage.out
	@find . -type d -name '__pycache__' -not -path './.git/*' -prune -exec rm -rf {} + 2>/dev/null || true
	@printf 'clean: build output removed\n'


.PHONY: lock-backend lock-tools worker

worker: ## Run the ARQ worker against the same Settings the API uses (design §4.6)
	@printf '==> worker: arq src.worker.WorkerSettings\n'
	@cd backend && arq src.worker.WorkerSettings
lock-backend: ## Regenerate hash-pinned backend lockfiles from pyproject.toml
	@printf '==> lock-backend: regenerating requirements.lock and requirements-dev.lock\n'
	@sh scripts/lock-backend.sh

lock-tools: ## Regenerate the hash-pinned CI tooling lock from requirements-tools.in
	@printf '==> lock-tools: regenerating scripts/requirements-tools.lock\n'
	@cd $(CURDIR) && pip-compile --generate-hashes --allow-unsafe --strip-extras \
		--output-file=scripts/requirements-tools.lock scripts/requirements-tools.in


# ─── Frontend targets (task 6) ─────────────────────────────────────────────
.PHONY: test-frontend lint-frontend build-frontend e2e-frontend load

test-frontend: ## Run frontend unit tests (single-run mode)
	@printf '==> test-frontend: vitest --run\n'
	@cd frontend && npx vitest --run

lint-frontend: ## Lint frontend with eslint + prettier
	@printf '==> lint-frontend: eslint + prettier\n'
	@cd frontend && npx eslint . && npx prettier --check .

build-frontend: ## Build the Next.js frontend
	@printf '==> build-frontend: next build\n'
	@cd frontend && npx next build

e2e-frontend: ## Run Playwright e2e tests
	@printf '==> e2e-frontend: playwright test\n'
	@cd frontend && npx playwright test

load: ## Run k6 load test (non-gating, requires k6 installed)
	@printf '==> load: k6 health smoke test (non-gating)\n'
	@command -v k6 >/dev/null 2>&1 && k6 run frontend/load/health.js || printf 'SKIP: k6 not installed\n'


# ─── Default-profile Compose lifecycle (task 7.1, design.md §13.3–§13.4) ────
# `up` has init-env as an explicit prerequisite, but a fresh clone can also run
# `docker compose up -d --wait` directly: every service loads the committed
# .env.example as a required env file and .env only as an optional override.
# No target ever passes --profile, so the vault and tools services stay out.
.PHONY: up down logs smoke

# `init-ca` AS WELL AS `init-env`, because the stack now contains a service that cannot start without
# an internal CA. `backend-agent` serves the agent's mutual-TLS port and issues its server certificate
# from `INTERNAL_CA_CERT_PEM`/`INTERNAL_CA_KEY_PEM`; with neither set it exits 2 and says so. That is
# the right behaviour — a listener must not invent trust material and hope nobody notices it is
# self-signed by itself — but it made `make up` on a fresh clone start a stack that could not deliver
# an approved change set.
#
# Safe to run every time: `init_ca.py` never overwrites an existing CA.
up: init-env init-ca ## Start the default profile and wait until /health/ready answers
	@printf '==> up: starting the default Compose profile and polling readiness\n'
	@sh scripts/dev-up.sh

down: ## Stop the default profile (containers only; volumes are preserved)
	@printf '==> down: docker compose down (unprofiled)\n'
	@docker compose down

logs: ## Follow the last 100 log lines of the default profile
	@docker compose logs -f --tail=100

smoke: ## Bounded liveness + readiness probe against a running backend
	@printf '==> smoke: probing liveness and readiness (bounded)\n'
	@FORGEOPS_SKIP_COMPOSE=1 FORGEOPS_READY_TIMEOUT=30 FORGEOPS_READY_INTERVAL=2 \
		sh scripts/dev-up.sh

# ─── Formatting (task 15.6, design.md §13.4) ────────────────────────────────
# Rewrites files. Never touches the four authoritative root documents: the Go
# and Python globs are component-scoped and prettier inherits the pre-commit
# four-document exclusion via --ignore-path.
.PHONY: fmt
fmt: ## Format Go, Python and frontend sources in place
	@printf '==> fmt: gofmt -w agent/\n'
	@cd agent && gofmt -w .
	@printf '==> fmt: ruff format backend/\n'
	@cd backend && ruff format src/ tests/ || true
	@printf '==> fmt: prettier --write frontend/\n'
	@cd frontend && npx prettier --write . || true

# ─── OpenTofu provider lock integrity (task 15.6, design.md §10.6) ──────────
.PHONY: check-tofu-lock
check-tofu-lock: ## Verify the committed six-platform null-provider lock has not drifted
	@printf '==> check-tofu-lock: tofu init -lockfile=readonly + six-platform freshness\n'
	@sh scripts/check-tofu-lock.sh

# ─── Alembic migrations (task 15.6, design.md §13.4) ────────────────────────
.PHONY: migrate migrate-new
migrate: ## Apply all migrations (alembic upgrade head) inside the backend container
	@printf '==> migrate: alembic upgrade head\n'
	@docker compose run --rm backend alembic upgrade head

migrate-new: ## Create a new autogenerated revision; requires m="summary"
	@if [ -z "$(m)" ]; then printf 'migrate-new: m is required, e.g. make migrate-new m="add table"\n' >&2; exit 1; fi
	@printf '==> migrate-new: alembic revision --autogenerate -m "%s"\n' '$(m)'
	@docker compose run --rm backend alembic revision --autogenerate -m "$(m)"

# ─── Audit chain (task 7.6, design.md §11.9, §13.4, Appendix A.8) ────────────
.PHONY: verify-chain audit-chain-smoke
verify-chain: ## Recompute the audit hash chain and report the first divergence (§11.9)
	@printf '==> verify-chain: recomputing audit_events from seq %s\n' '$(if $(since),$(since),0)'
	@docker compose run --rm backend python -m src.audit.verify_cli \
		$(if $(tenant),--tenant $(tenant),) $(if $(since),--since $(since),) \
		$(if $(rows),--require-rows $(rows),)

audit-chain-smoke: ## Seed the chain, verify it, then tamper one row and require the objection (D-69)
	@printf '==> audit-chain-smoke: write, verify, tamper, verify, restore\n'
	@docker compose run --rm -v "$(CURDIR)/scripts:/smoke:ro" backend python /smoke/audit-chain-smoke.py

# ─── Supply chain (tasks 15.6 + 15.8, design.md §8.1–§8.2, §13.4) ───────────
.PHONY: sbom release-snapshot verify-release
sbom: ## Generate a CycloneDX SBOM for the agent build (criterion 15)
	@printf '==> sbom: syft CycloneDX JSON for the agent\n'
	@sh scripts/sbom.sh

release-snapshot: ## Validate the release config without publishing (criterion 7)
	@printf '==> release-snapshot: goreleaser release --snapshot --clean\n'
	@cd agent && goreleaser release --snapshot --clean

verify-release: ## cosign verify-blob + SBOM presence on a published artifact (criterion 16)
	@printf '==> verify-release: cosign verify-blob + SBOM presence check\n'
	@sh scripts/verify-release.sh

# ─── Policy bundles (task 9.1, design.md §8.3, §11.7, §13.4) ────────────────
.PHONY: policy-test
policy-test: ## opa check --strict + opa test over every Rego bundle (criterion 7)
	@printf '==> policy-test: opa check --strict + opa test over policies/\n'
	@bash scripts/policy-test.sh

# ─── End-to-end (task 15.6) ────────────────────────────────────────────────
.PHONY: e2e
e2e: ## Run Playwright e2e against a built frontend + running backend
	@printf '==> e2e: playwright test\n'
	@cd frontend && npx playwright test
