#!/usr/bin/env bash
# scripts/check-go-module.sh — Go module policy assertions
# Checks: exact module path, go 1.26, -mod=readonly, SPDX headers,
#          no forbidden placeholder packages, tree-sitter absent, nhooyr.io/websocket absent.
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "$0")/../agent" && pwd)"
ERRORS=0

err() { echo "FAIL: $1" >&2; ERRORS=$((ERRORS + 1)); }

# 1. Exact module path
MOD_PATH=$(sed -n 's/^module //p' "$AGENT_DIR/go.mod" | tr -d '\r')
if [ "$MOD_PATH" != "github.com/parag8487/ForgeOps/agent" ]; then
  err "Module path is '$MOD_PATH', expected 'github.com/parag8487/ForgeOps/agent'"
fi

# 2. Go 1.26 directive
GO_VER=$(sed -n 's/^go //p' "$AGENT_DIR/go.mod" | tr -d '\r')
if [ "$GO_VER" != "1.26" ]; then
  err "Go directive is '$GO_VER', expected '1.26'"
fi

# 3. -mod=readonly (verify go mod download works in readonly mode)
export PATH="C:/IMP/kiro/_toolchain/go/bin:$PATH"
if ! (cd "$AGENT_DIR" && GOFLAGS="-mod=readonly" go build ./... 2>/dev/null); then
  # It's okay if build fails because code doesn't exist yet, just check mod readonly
  if ! (cd "$AGENT_DIR" && GOFLAGS="-mod=readonly" go mod verify 2>/dev/null); then
    err "-mod=readonly verification failed"
  fi
fi

# 4. SPDX-License-Identifier: Apache-2.0 as first line of every .go file
GO_FILES=$(find "$AGENT_DIR" -name '*.go' -not -path '*/vendor/*' 2>/dev/null || true)
if [ -n "$GO_FILES" ]; then
  while IFS= read -r f; do
    FIRST_LINE=$(head -1 "$f" | tr -d '\r')
    if [ "$FIRST_LINE" != "// SPDX-License-Identifier: Apache-2.0" ]; then
      err "Missing SPDX header in $f (first line: '$FIRST_LINE')"
    fi
  done <<< "$GO_FILES"
fi

# 5. No forbidden placeholder packages (structural dirs should have no .go files)
#
# The list is PHASE-SCOPED and must shrink as directories are populated, or this rule becomes
# pattern O — a check frozen at a previous phase whose findings are noise. It read
# `executor validator policy devtools` until leaf 7.10, and `internal/executor` has held real code
# since leaf 7.2 put the mutation boundary under it (design §2.2.1 mechanism 3, D-45). The check
# had therefore been failing since then, and nothing noticed because it is wired into neither CI
# nor pre-commit nor `make lint` — a stale check that nobody runs.
#
# That prediction came true exactly as written. `validator` was populated by group 14 and `policy`
# by leaf 9.4 — 12 and 4 `.go` files respectively — and neither leaf removed its name here, so the
# rule had six findings, all of them noise, and still nothing noticed because nothing ran it. Both
# names move to 5b in this pass, which asserts the opposite and would have caught the omission the
# day either directory was populated. `devtools` genuinely remains structural: the directory exists
# and holds no `.go` file.
for dir in devtools; do
	FOUND=$(find "$AGENT_DIR/internal/$dir" -name '*.go' 2>/dev/null || true)
	if [ -n "$FOUND" ]; then
		err "Forbidden .go file(s) in structural directory internal/$dir: $FOUND"
	fi
done

# 5b. And the other direction, which is what stops 5 rotting again: a directory that has LEFT the
# structural list must actually hold code. Without this, removing a name from the loop above would
# be indistinguishable from deleting the rule.
for dir in executor envelope session validator policy; do
	FOUND=$(find "$AGENT_DIR/internal/$dir" -name '*.go' 2>/dev/null || true)
	if [ -z "$FOUND" ]; then
		err "internal/$dir is no longer treated as structural but holds no .go file"
	fi
done

# 6. tree-sitter absent from go.mod AND go.sum (decision D-1)
GOMOD_CONTENT=$(cat "$AGENT_DIR/go.mod" | tr -d '\r')
GOSUM_FILE="$AGENT_DIR/go.sum"

if echo "$GOMOD_CONTENT" | grep -q "tree-sitter"; then
  err "tree-sitter/go-tree-sitter found in go.mod (violates D-1)"
fi

if [ -f "$GOSUM_FILE" ]; then
  GOSUM_CONTENT=$(cat "$GOSUM_FILE" | tr -d '\r')
  if echo "$GOSUM_CONTENT" | grep -q "tree-sitter"; then
    err "tree-sitter found in go.sum (violates D-1)"
  fi
fi

# 7. nhooyr.io/websocket absent
if echo "$GOMOD_CONTENT" | grep -q "nhooyr.io/websocket"; then
  err "nhooyr.io/websocket found in go.mod (deprecated, use coder/websocket)"
fi

if [ -f "$GOSUM_FILE" ] && cat "$GOSUM_FILE" | tr -d '\r' | grep -q "nhooyr.io/websocket"; then
  err "nhooyr.io/websocket found in go.sum (deprecated)"
fi

# Summary
if [ $ERRORS -gt 0 ]; then
  echo "check-go-module: $ERRORS error(s) found" >&2
  exit 1
fi
echo "check-go-module: all checks passed"
exit 0
