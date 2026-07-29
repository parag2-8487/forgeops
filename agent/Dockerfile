# SPDX-License-Identifier: Apache-2.0
# ForgeOps Agent Dockerfile
#
# Two targets:
#   builder  — compiles the agent binary (used in CI/release)
#   devtools — carries OpenTofu 1.12.5 for integration tests (profile: tools)

# Build stage
FROM golang:1.26-alpine AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/forgeops-agent ./cmd/agent

# Devtools target — carries OpenTofu for IaC integration tests.
# This is the Compose `agent-dev` service (profile: tools).
FROM ghcr.io/opentofu/opentofu:1.12.5 AS devtools
RUN apk add --no-cache bash git curl
ENV TF_PLUGIN_CACHE_DIR=/var/cache/tofu/plugins
RUN mkdir -p /var/cache/tofu/plugins
WORKDIR /workspace
