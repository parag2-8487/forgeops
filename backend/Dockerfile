# SPDX-License-Identifier: FSL-1.1-ALv2
# ForgeOps backend multi-stage Dockerfile (design.md §7.7, §13.3).
# Installs ONLY requirements.lock with pip install --require-hashes.
# Runs as non-root. Exposes a `runtime` target.

# ---------- Stage 1: builder ----------
FROM python:3.13-slim AS builder

WORKDIR /app

# Install only the production lock with hash verification.
COPY requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Copy source for the editable install
COPY pyproject.toml ./
COPY src/ ./src/
# `config/` holds model-tiers.yaml and mcp-servers.yaml. It was never copied into
# the image, and nothing noticed: `load_mcp_server_config` returns an EMPTY registry
# for a missing file — correct fail-closed behaviour for a gateway, and completely
# invisible, so every containerised `tools/list` returned nothing by design rather
# than by accident. Task 2.1 wired `load_tier_config` into the lifespan, which turned
# the same missing directory into a startup crash and surfaced it (design §0.5 D1).
COPY config/ ./config/
COPY alembic.ini ./
COPY alembic/ ./alembic/

RUN pip install --no-deps -e .

# ---------- Stage 2: runtime ----------
FROM python:3.13-slim AS runtime

# The OPA binary, for `POST /api/v1/policies/{id}/test` and `policies/validation.py::validate_rego`.
#
# WHY IT IS HERE AT ALL. The dry-run route used to answer with a SYNTHESISED allow/deny when
# `shutil.which("opa")` found nothing — a verdict on a security surface that no policy engine had
# computed. It now raises `503 dryrun-unavailable` instead, which is honest but useless if the
# binary is never present, so the image that serves the route has to carry it.
#
# COPIED from the same digest-pinned image `docker-compose.yml` already runs, rather than curled
# from a release page. A download would add a second supply-chain surface needing its own checksum;
# this reference is already committed, already reviewed, and already the version `scripts/policy-test.sh`
# greps out of Compose to run the Rego test suite — so the evaluator inside the API and the evaluator
# the policy tests run against cannot drift apart.
COPY --from=openpolicyagent/opa:1.4.2@sha256:35a093d9ae828373cf88f68ecaa8189ab26287468074a3b78f0601d9c8b7a4f5 \
	/opa /usr/local/bin/opa

# Security: run as non-root
RUN groupadd -r forgeops && useradd --no-log-init -r -g forgeops forgeops

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY --from=builder /usr/local/bin/alembic /usr/local/bin/alembic

# Copy application source
COPY --from=builder /app/src ./src
COPY --from=builder /app/config ./config
COPY --from=builder /app/alembic.ini ./
COPY --from=builder /app/alembic ./alembic
COPY --from=builder /app/pyproject.toml ./

# Note: requirements-dev.lock is NOT copied into the runtime image.
# Only requirements.lock is installed in the builder stage.

USER forgeops

EXPOSE 8000

CMD ["uvicorn", "src.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
