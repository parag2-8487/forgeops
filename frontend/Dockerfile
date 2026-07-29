# ForgeOps Frontend — Multi-stage Dockerfile
# Design: §12.6, §13.3
#
# NEXT_PUBLIC_* variables are inlined at build time by Next.js.
# Setting them only at runtime is explicitly insufficient — they must be
# declared as ARG, converted to ENV in the builder stage, and present
# BEFORE `pnpm build` runs.

# ─── Stage 1: deps ──────────────────────────────────────────────────────────
FROM node:22-alpine AS deps
WORKDIR /app

RUN corepack enable && corepack prepare pnpm@latest --activate

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# ─── Stage 2: builder ───────────────────────────────────────────────────────
FROM node:22-alpine AS builder
WORKDIR /app

RUN corepack enable && corepack prepare pnpm@latest --activate

# CRITICAL: Both public variables MUST be declared as ARG and converted to ENV
# BEFORE pnpm build. NEXT_PUBLIC_* is inlined into the client bundle at build
# time — runtime-only environment variables are NOT sufficient.
ARG NEXT_PUBLIC_API_BASE_URL
ARG NEXT_PUBLIC_APP_NAME

# Convert ARG to ENV so they are visible to the `pnpm build` process.
# This MUST happen BEFORE the build step.
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}
ENV NEXT_PUBLIC_APP_NAME=${NEXT_PUBLIC_APP_NAME}

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Build with the public variables already in the environment
RUN pnpm build

# ─── Stage 3: runtime (production) ──────────────────────────────────────────
FROM node:22-alpine AS runtime
WORKDIR /app

# Run as non-root for security
RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copy only the production output
COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static

USER nextjs

EXPOSE 3000

ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

CMD ["node", "server.js"]
