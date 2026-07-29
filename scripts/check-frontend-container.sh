#!/usr/bin/env bash
# scripts/check-frontend-container.sh
# Static assertions for frontend/Dockerfile and docker-compose.yml frontend service.
# Validates that NEXT_PUBLIC_* variables are introduced at BUILD TIME (before pnpm build),
# not only at runtime. Design: §12.6, §13.3, Appendix E criterion 6.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCKERFILE="$REPO_ROOT/frontend/Dockerfile"
COMPOSEFILE="$REPO_ROOT/docker-compose.yml"

errors=0

echo "==> Checking frontend/Dockerfile build-time variable assertions..."

# 1. Dockerfile must declare BOTH ARGs
if ! grep -F "ARG NEXT_PUBLIC_API_BASE_URL" "$DOCKERFILE" > /dev/null 2>&1; then
  echo "FAIL: Dockerfile missing ARG NEXT_PUBLIC_API_BASE_URL"
  errors=$((errors + 1))
fi
if ! grep -F "ARG NEXT_PUBLIC_APP_NAME" "$DOCKERFILE" > /dev/null 2>&1; then
  echo "FAIL: Dockerfile missing ARG NEXT_PUBLIC_APP_NAME"
  errors=$((errors + 1))
fi

# 2. Dockerfile must convert ARGs to ENV in builder stage BEFORE pnpm build
# Strategy: Find "ENV NEXT_PUBLIC_API_BASE_URL" and "ENV NEXT_PUBLIC_APP_NAME"
# and verify they come BEFORE the "RUN pnpm build" line
TMPF_DOCKERFILE="$REPO_ROOT/.tmp_dockerfile_check"
tr -d '\r' < "$DOCKERFILE" > "$TMPF_DOCKERFILE"

# Get line numbers
line_env_url=$(grep -n "ENV NEXT_PUBLIC_API_BASE_URL" "$TMPF_DOCKERFILE" | head -1 | cut -d: -f1)
line_env_name=$(grep -n "ENV NEXT_PUBLIC_APP_NAME" "$TMPF_DOCKERFILE" | head -1 | cut -d: -f1)
line_build=$(grep -n "RUN pnpm build" "$TMPF_DOCKERFILE" | head -1 | cut -d: -f1)
line_builder_stage=$(grep -n "AS builder" "$TMPF_DOCKERFILE" | head -1 | cut -d: -f1)
line_runtime_stage=$(grep -n "AS runtime" "$TMPF_DOCKERFILE" | head -1 | cut -d: -f1)

if [ -z "$line_env_url" ]; then
  echo "FAIL: Dockerfile missing ENV NEXT_PUBLIC_API_BASE_URL"
  errors=$((errors + 1))
else
  if [ -z "$line_build" ]; then
    echo "FAIL: Dockerfile missing RUN pnpm build"
    errors=$((errors + 1))
  elif [ "$line_env_url" -ge "$line_build" ]; then
    echo "FAIL: ENV NEXT_PUBLIC_API_BASE_URL (line $line_env_url) must come BEFORE pnpm build (line $line_build)"
    errors=$((errors + 1))
  else
    echo "  OK: ENV NEXT_PUBLIC_API_BASE_URL (line $line_env_url) is before pnpm build (line $line_build)"
  fi
fi

if [ -z "$line_env_name" ]; then
  echo "FAIL: Dockerfile missing ENV NEXT_PUBLIC_APP_NAME"
  errors=$((errors + 1))
else
  if [ -z "$line_build" ]; then
    echo "FAIL: Dockerfile missing RUN pnpm build"
    errors=$((errors + 1))
  elif [ "$line_env_name" -ge "$line_build" ]; then
    echo "FAIL: ENV NEXT_PUBLIC_APP_NAME (line $line_env_name) must come BEFORE pnpm build (line $line_build)"
    errors=$((errors + 1))
  else
    echo "  OK: ENV NEXT_PUBLIC_APP_NAME (line $line_env_name) is before pnpm build (line $line_build)"
  fi
fi

# 3. ENV conversions must be in the builder stage (between builder and runtime)
if [ -n "$line_env_url" ] && [ -n "$line_builder_stage" ] && [ -n "$line_runtime_stage" ]; then
  if [ "$line_env_url" -le "$line_builder_stage" ] || [ "$line_env_url" -ge "$line_runtime_stage" ]; then
    echo "FAIL: ENV NEXT_PUBLIC_API_BASE_URL is NOT in the builder stage"
    errors=$((errors + 1))
  else
    echo "  OK: ENV NEXT_PUBLIC_API_BASE_URL is in the builder stage"
  fi
fi
if [ -n "$line_env_name" ] && [ -n "$line_builder_stage" ] && [ -n "$line_runtime_stage" ]; then
  if [ "$line_env_name" -le "$line_builder_stage" ] || [ "$line_env_name" -ge "$line_runtime_stage" ]; then
    echo "FAIL: ENV NEXT_PUBLIC_APP_NAME is NOT in the builder stage"
    errors=$((errors + 1))
  else
    echo "  OK: ENV NEXT_PUBLIC_APP_NAME is in the builder stage"
  fi
fi

# 4. Runtime stage must be named "runtime"
if ! grep -F "AS runtime" "$TMPF_DOCKERFILE" > /dev/null 2>&1; then
  echo "FAIL: Dockerfile missing runtime stage named 'runtime'"
  errors=$((errors + 1))
else
  echo "  OK: Runtime stage named 'runtime'"
fi

# 5. Non-root user in runtime stage
line_user=$(grep -n "^USER " "$TMPF_DOCKERFILE" | tail -1 | cut -d: -f1)
if [ -z "$line_user" ]; then
  echo "FAIL: Dockerfile runtime stage has no USER directive (non-root required)"
  errors=$((errors + 1))
elif [ -n "$line_runtime_stage" ] && [ "$line_user" -gt "$line_runtime_stage" ]; then
  echo "  OK: Non-root USER set in runtime stage"
else
  echo "FAIL: USER directive not found after runtime stage start"
  errors=$((errors + 1))
fi

rm -f "$TMPF_DOCKERFILE"

echo ""
echo "==> Checking docker-compose.yml frontend service..."

TMPF_COMPOSE="$REPO_ROOT/.tmp_compose_check"
tr -d '\r' < "$COMPOSEFILE" > "$TMPF_COMPOSE"

# 6. Frontend service exists with correct build context and target
if ! grep -F "context: ./frontend" "$TMPF_COMPOSE" > /dev/null 2>&1; then
  echo "FAIL: docker-compose.yml missing frontend build context ./frontend"
  errors=$((errors + 1))
else
  echo "  OK: Frontend build context is ./frontend"
fi

if ! grep -F "target: runtime" "$TMPF_COMPOSE" > /dev/null 2>&1; then
  echo "FAIL: docker-compose.yml missing target: runtime for frontend"
  errors=$((errors + 1))
else
  echo "  OK: Frontend build target is runtime"
fi

# 7. Build args include both public vars with correct defaults
if ! grep -F "NEXT_PUBLIC_API_BASE_URL:" "$TMPF_COMPOSE" > /dev/null 2>&1; then
  echo "FAIL: docker-compose.yml missing NEXT_PUBLIC_API_BASE_URL build arg"
  errors=$((errors + 1))
else
  echo "  OK: NEXT_PUBLIC_API_BASE_URL build arg present"
fi

if ! grep -F "NEXT_PUBLIC_APP_NAME:" "$TMPF_COMPOSE" > /dev/null 2>&1; then
  echo "FAIL: docker-compose.yml missing NEXT_PUBLIC_APP_NAME build arg"
  errors=$((errors + 1))
else
  echo "  OK: NEXT_PUBLIC_APP_NAME build arg present"
fi

# 8. Correct defaults for browser-safe URLs
if ! grep -F "http://localhost:8000/api/v1" "$TMPF_COMPOSE" > /dev/null 2>&1; then
  echo "FAIL: docker-compose.yml missing default http://localhost:8000/api/v1"
  errors=$((errors + 1))
else
  echo "  OK: Default API URL is http://localhost:8000/api/v1 (browser-safe)"
fi

if ! grep -F "ForgeOps" "$TMPF_COMPOSE" > /dev/null 2>&1; then
  echo "FAIL: docker-compose.yml missing default ForgeOps app name"
  errors=$((errors + 1))
else
  echo "  OK: Default app name is ForgeOps"
fi

# 9. Loopback port binding
if ! grep -F '127.0.0.1:${FRONTEND_PORT:-3000}:3000' "$TMPF_COMPOSE" > /dev/null 2>&1; then
  echo "FAIL: docker-compose.yml frontend port not loopback-only 127.0.0.1:\${FRONTEND_PORT:-3000}:3000"
  errors=$((errors + 1))
else
  echo "  OK: Frontend port is loopback-only 127.0.0.1:\${FRONTEND_PORT:-3000}:3000"
fi

# 10. depends_on backend with condition service_healthy
if ! grep -F "service_healthy" "$TMPF_COMPOSE" > /dev/null 2>&1; then
  echo "FAIL: docker-compose.yml frontend missing depends_on backend condition service_healthy"
  errors=$((errors + 1))
else
  echo "  OK: Frontend depends_on backend condition service_healthy"
fi

# 11. env_file uses the x-service-env anchor
if grep -F "frontend:" "$TMPF_COMPOSE" > /dev/null 2>&1; then
  # Check that there's an env_file entry after the frontend service
  frontend_section=$(sed -n '/^  frontend:/,/^  [a-z]/p' "$TMPF_COMPOSE" | head -30)
  if echo "$frontend_section" | grep -F "env_file:" > /dev/null 2>&1; then
    echo "  OK: Frontend service has env_file"
  else
    echo "FAIL: Frontend service missing env_file"
    errors=$((errors + 1))
  fi
fi

# 12. No vault or tools profiles on the frontend
if grep -F "frontend:" "$TMPF_COMPOSE" > /dev/null 2>&1; then
  frontend_section=$(sed -n '/^  frontend:/,/^  [a-z]/p' "$TMPF_COMPOSE" | head -30)
  if echo "$frontend_section" | grep -F "profiles:" > /dev/null 2>&1; then
    echo "FAIL: Frontend service must NOT have profiles (vault/tools forbidden)"
    errors=$((errors + 1))
  else
    echo "  OK: Frontend service has no profiles"
  fi
fi

rm -f "$TMPF_COMPOSE"

# ── 13. The generated client bundle actually carries the build-time URL ──────
#
# Assertions 1-12 read the Dockerfile and docker-compose.yml. Design §13.3 asks
# for more than that: "a container-build test inspects/executes the generated
# client bundle and proves requests use the supplied browser-reachable URL rather
# than a server-internal hostname or runtime-only value." A static grep cannot
# tell the difference between a value Next.js inlines and one it reads at runtime,
# which is the whole point of the criterion.
#
# So when a build output exists, grep it. NEXT_PUBLIC_API_BASE_URL names the URL
# the build was given; the emitted client chunks must contain it.
BUILD_DIR="$REPO_ROOT/frontend/.next"

if [ -d "$BUILD_DIR" ]; then
  echo ""
  echo "==> Inspecting the generated client bundle in frontend/.next ..."
  expected="${NEXT_PUBLIC_API_BASE_URL:-}"
  if [ -z "$expected" ]; then
    echo "FAIL: NEXT_PUBLIC_API_BASE_URL is not set, so the inlined value cannot be verified"
    echo "      Re-run with the same value the build used, e.g."
    echo "      NEXT_PUBLIC_API_BASE_URL=http://ci.example.test:9999/api/v1 bash scripts/check-frontend-container.sh"
    errors=$((errors + 1))
  elif grep -rqF "$expected" "$BUILD_DIR/static" 2>/dev/null; then
    echo "  OK: the client bundle contains the build-time URL ($expected)"
  else
    echo "FAIL: the client bundle does NOT contain the build-time URL ($expected)"
    echo "      NEXT_PUBLIC_* must be inlined at build time; a runtime-only value is insufficient."
    errors=$((errors + 1))
  fi

  # A server-internal Compose hostname must never reach browser code.
  if grep -rqE 'https?://(backend|api|opa|postgres|redis|frontend):[0-9]+' "$BUILD_DIR/static" 2>/dev/null; then
    echo "FAIL: the client bundle references an internal Compose hostname"
    errors=$((errors + 1))
  else
    echo "  OK: no internal Compose hostname in the client bundle"
  fi
else
  echo ""
  echo "==> frontend/.next not present: skipping the bundle inspection."
  echo "    Static assertions above still ran. Build first for full criterion 6 evidence:"
  echo "    (cd frontend && NEXT_PUBLIC_API_BASE_URL=http://ci.example.test:9999/api/v1 pnpm exec next build)"
  if [ -n "${FORGEOPS_REQUIRE_BUNDLE_CHECK:-}" ]; then
    echo "FAIL: FORGEOPS_REQUIRE_BUNDLE_CHECK is set but frontend/.next does not exist"
    errors=$((errors + 1))
  fi
fi

echo ""
if [ "$errors" -eq 0 ]; then
  echo "OK: frontend/Dockerfile and docker-compose.yml pass all Phase 0 container assertions"
  exit 0
else
  echo "FAIL: $errors assertion(s) failed"
  exit 1
fi
