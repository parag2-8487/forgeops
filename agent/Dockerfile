# SPDX-License-Identifier: Apache-2.0
# ForgeOps Agent Dockerfile
#
# Three targets:
#   builder  — compiles the agent binary (used in CI/release)
#   runtime  — the agent itself, run as a container by docker-compose.e2e.yml
#   devtools — carries OpenTofu 1.12.5 for integration tests (profile: tools)

# Build stage
FROM golang:1.26-alpine AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/forgeops-agent ./cmd/agent

# Runtime target — the real agent binary, run as a container by docker-compose.e2e.yml.
#
# Added for criterion 10: §8.3.2 requires the journey to build "the real agent binary and run it as
# a container with a fixture Node.js project mounted", and no target ran the agent. `devtools`
# carries OpenTofu and sleeps forever; it is a test harness, not the agent.
#
# Alpine rather than scratch or distroless, deliberately. The agent shells out to `git`, and the
# journey needs `docker compose exec` to work in order to pair — neither of which a scratch image
# supports. The tradeoff is a shell in the image, acceptable for a CI fixture and not for a
# release artifact.
FROM alpine:3.22 AS runtime
RUN apk add --no-cache ca-certificates git
# Non-root: the agent writes only to its state directory and the workspace, so running it as root
# in the journey would mean the journey never exercises the permissions a real install has.
RUN addgroup -S forgeops && adduser -S -G forgeops forgeops \
    && mkdir -p /var/lib/forgeops /workspace \
    && chown -R forgeops:forgeops /var/lib/forgeops /workspace
COPY --from=builder /out/forgeops-agent /usr/local/bin/forgeops-agent
USER forgeops
WORKDIR /workspace
ENV AGENT_STATE_DIR=/var/lib/forgeops \
    AGENT_CREDENTIAL_STORE=file
# Idles by default. The journey mints a pairing code through the API, execs `pair --code`, then
# execs `run` — three separately checkable exit statuses rather than one opaque entrypoint that
# either works or does not.
CMD ["sleep", "infinity"]

# Devtools target — carries OpenTofu for IaC integration tests.
# This is the Compose `agent-dev` service (profile: tools).
FROM ghcr.io/opentofu/opentofu:1.12.5 AS devtools
RUN apk add --no-cache bash git curl
ENV TF_PLUGIN_CACHE_DIR=/var/cache/tofu/plugins
RUN mkdir -p /var/cache/tofu/plugins
WORKDIR /workspace
