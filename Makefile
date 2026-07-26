# ForgeOps root Makefile — Phase 0 initial contracts (design.md §13.4).
#
# Requires GNU make and a POSIX shell (Git Bash or WSL2 on Windows).
# Business logic lives in scripts/*.sh so the same commands work everywhere.
#
# Only the initial four targets are defined here: help (default goal),
# bootstrap, init-env and clean. The remaining §13.4 targets (build/test/lint,
# up/down, locks, sbom, release) are wired by their owning implementation tasks.

SHELL := /bin/sh
.DEFAULT_GOAL := help

# Pinned toolchain versions verified by bootstrap (design.md §16.2, §16.4).
DOCKER_COMPOSE_VERSION := 2.24.7
PIP_TOOLS_VERSION := 7.4.1

.PHONY: help bootstrap init-env clean

help: ## List the available targets
	@printf 'ForgeOps — Phase 0 make targets\n\n'
	@awk 'BEGIN { FS = ":[^#]*## " } /^[a-zA-Z0-9_.-]+:[^#]*## / { printf "  %-14s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf '\nRequires GNU make and a POSIX shell. See docs/development.md.\n'

bootstrap: ## Verify the pinned toolchain; never rewrites lockfiles
	@printf '==> bootstrap: verifying pinned toolchain (Docker Compose %s, pip-tools==%s)\n' '$(DOCKER_COMPOSE_VERSION)' '$(PIP_TOOLS_VERSION)'
	@FORGEOPS_DOCKER_COMPOSE_VERSION='$(DOCKER_COMPOSE_VERSION)' \
		FORGEOPS_PIP_TOOLS_VERSION='$(PIP_TOOLS_VERSION)' \
		sh scripts/bootstrap.sh

init-env: ## Create .env from .env.example only when .env is absent
	@printf '==> init-env: ensuring a local .env exists without overwriting one\n'
	@sh scripts/init-env.sh

clean: ## Remove build output only; never .env, lockfiles or Docker volumes
	@printf '==> clean: removing build output (preserving .env, lockfiles and Docker volumes)\n'
	@rm -rf dist agent/dist frontend/.next frontend/out frontend/coverage
	@rm -rf backend/htmlcov backend/coverage.xml backend/.coverage backend/.pytest_cache
	@rm -rf .pytest_cache .ruff_cache backend/.ruff_cache agent/coverage.out
	@find . -type d -name '__pycache__' -not -path './.git/*' -prune -exec rm -rf {} + 2>/dev/null || true
	@printf 'clean: build output removed\n'
