# Leaf 10.1 Agent Secret Scanning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement agent-side secret scanning and redaction via `agent/internal/secretscan` wrapping `gitleaks/v8`.

**Architecture:** A `Scanner` interface scans files/content and returns `Finding` metadata (never values). A `Redact` function creates `RedactedChunk` types by replacing secrets with `FORGEOPS_REDACTED:<kind>:<hash8>`. Validator diagnostics are routed through `Redact`.

**Tech Stack:** Go 1.22, `github.com/zricethezav/gitleaks/v8`.

## Global Constraints

- `github.com/zricethezav/gitleaks/v8 v8.30.1` MUST be used.
- `Finding` MUST NOT return matched values.
- `FORGEOPS_REDACTED:<kind>:<hash8>` format MUST be used. `hash8` is the first 8 hex of `HMAC-SHA256(project_pepper, value)`.
- Tests MUST prove no value is returned, logged, or transmitted.

---

### Task 1: Scaffolding and Interface Definitions

**Files:**

- Create: `agent/internal/secretscan/scanner.go`
- Create: `agent/internal/secretscan/scanner_test.go`

**Interfaces:**

- Produces: `Scanner` interface, `Finding` struct, `RedactedChunk` struct, `Redact(ctx, chunk, findings)` function signature.

- [ ] **Step 1: Write the failing tests** for `Finding` and `RedactedChunk` structures.
- [ ] **Step 2: Implement the minimal code** defining the interfaces and structs exactly as specified in `design.md`.
- [ ] **Step 3: Run the tests** and ensure compilation/tests pass.
- [ ] **Step 4: Commit**.

### Task 2: Implement Gitleaks Wrapper (Scanner)

**Files:**

- Modify: `agent/internal/secretscan/scanner.go`
- Modify: `agent/internal/secretscan/scanner_test.go`
- Create: `agent/internal/secretscan/testdata/synthetic_credentials.txt` (or similar)

**Interfaces:**

- Consumes: `Scanner` interface.
- Produces: A concrete `gitleaksScanner` that executes `github.com/zricethezav/gitleaks/v8` (v8.30.1).

- [ ] **Step 1: Write the failing test** that loads a synthetic test file and asserts `Scan` finds the credentials but DOES NOT return the literal value.
- [ ] **Step 2: Implement `gitleaksScanner`** that uses the gitleaks library. Ensure it zeroes out the match value after extracting `Kind`, `Path`, `Line`, and `Entropy`.
- [ ] **Step 3: Run the tests** and make sure they pass.
- [ ] **Step 4: Commit**.

### Task 3: Implement Redaction Engine

**Files:**

- Modify: `agent/internal/secretscan/scanner.go`
- Modify: `agent/internal/secretscan/scanner_test.go`

**Interfaces:**

- Consumes: `Finding`
- Produces: `Redact` function producing `RedactedChunk`.

- [ ] **Step 1: Write the failing test** asserting `Redact` replaces strings with `FORGEOPS_REDACTED:<kind>:<hash8>`.
- [ ] **Step 2: Implement `Redact`**. Hash using HMAC-SHA256(project_pepper, value).
- [ ] **Step 3: Run the tests**.
- [ ] **Step 4: Commit**.

### Task 4: Chokepoint Integration

**Files:**

- Modify: `agent/internal/executor/internal/mutate/apply.go` (or wherever validator diagnostics are routed before logging)
- Modify: relevant test files to assert redaction on diagnostics.

**Interfaces:**

- Consumes: `secretscan.Redact`

- [ ] **Step 1: Write the failing test** proving validator diagnostic output containing a secret leaks it without redaction.
- [ ] **Step 2: Implement the chokepoint**. Route validator diagnostics through `secretscan.Redact` before logging/transmission.
- [ ] **Step 3: Run the tests** asserting the output is redacted.
- [ ] **Step 4: Commit**.
