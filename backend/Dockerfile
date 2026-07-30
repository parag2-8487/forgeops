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
