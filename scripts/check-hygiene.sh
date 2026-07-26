#!/bin/sh
# Phase 0 ignore/pre-commit hygiene configuration check.
#
# Enforces design.md §0.3 (the four authoritative root documents are read-only
# inputs), §8.4 (the exact hook set and the single top-level exclusion) and
# §14.1 (Gitleaks is a gate, not an optional formatter).
#
# The two structural guarantees asserted here are:
#   1. The top-level pre-commit exclusion covers exactly the four authoritative
#      documents — no more, no less — so every filename-receiving (mutating) hook
#      skips them.
#   2. Gitleaks is exempt from that exclusion by construction: it receives no
#      filenames and always runs, so it still scans all four documents.
#
# Read-only: it never creates, moves, formats or deletes anything. It prints
# every violation it finds and exits non-zero if there is at least one.

set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT" || exit 1

CONFIG=.pre-commit-config.yaml
IGNORE=.gitignore

FAILFILE=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/forgeops-hygiene-$$")
: >"$FAILFILE"
FACTS=$(mktemp 2>/dev/null || printf '%s' "${TMPDIR:-/tmp}/forgeops-hooks-$$")
: >"$FACTS"
trap 'rm -f "$FAILFILE" "$FACTS"' EXIT HUP INT TERM

fail() {
	printf 'FAIL: %s\n' "$1" >&2
	printf 'x\n' >>"$FAILFILE"
}

# The four authoritative, read-only reference documents (design §0.3).
AUTHORITATIVE_DOCS='AI-Powered-DevOps-Platform-Complete-Technical-Research.md
PRD.md
Tech-Stack-Analysis.md
phases.md'

# Hooks that rewrite files, or that must otherwise honour the exclusion because
# pre-commit hands them filenames (design §8.4).
MUTATING_HOOKS='ruff
ruff-format
gofmt
go-vet
prettier
end-of-file-fixer
trailing-whitespace
check-merge-conflict
check-yaml
check-added-large-files'

# Tracked inputs that .gitignore must never ignore.
PROTECTED_PATHS='AI-Powered-DevOps-Platform-Complete-Technical-Research.md
PRD.md
Tech-Stack-Analysis.md
phases.md
README.md
LICENSE
agent/LICENSE
agent/NOTICE
.env.example
.pre-commit-config.yaml
Makefile
docker-compose.yml
backend/pyproject.toml
backend/requirements.lock
backend/requirements-dev.lock
frontend/package.json
frontend/pnpm-lock.yaml
agent/go.mod
agent/go.sum
agent/testdata/plan-sample.json
agent/testfixtures/tofu-null/.terraform.lock.hcl
config/model-tiers.yaml
policies/mcp/gateway.rego'

# Patterns .gitignore must carry: generated output, the local .env, caches and
# IDE state (design §0.3).
REQUIRED_IGNORES='.env
__pycache__/
.pytest_cache/
.ruff_cache/
node_modules/
.next/
dist/
.terraform/
.idea/'

if [ ! -f "$CONFIG" ]; then
	fail "$CONFIG is missing"
fi
if [ ! -f "$IGNORE" ]; then
	fail "$IGNORE is missing"
fi

# ── Flatten the hook configuration into repo/hook/key facts ──────────────────
if [ -f "$CONFIG" ]; then
	awk '
		/^  - repo:/ { repo = $3; hook = ""; next }
		/^    rev:/  { print "repo\t" repo "\trev\t" $2; next }
		/^      - id:/ {
			hook = $3
			print "repo\t" repo "\thook\t" hook
			next
		}
		hook != "" && /^        [a-z_]+:/ {
			line = $0
			sub(/^ +/, "", line)
			key = line
			sub(/:.*$/, "", key)
			val = line
			sub(/^[a-z_]+:[ ]*/, "", val)
			print "hook\t" hook "\t" key "\t" val
			next
		}
	' "$CONFIG" >"$FACTS"
fi

hook_key() { # hook_key <hook id> <key>  -> prints value, empty if unset
	awk -F'\t' -v h="$1" -v k="$2" '$1=="hook" && $2==h && $3==k { print $4 }' "$FACTS"
}

hook_exists() {
	awk -F'\t' -v h="$1" '$1=="repo" && $3=="hook" && $4==h { found=1 } END { exit found?0:1 }' "$FACTS"
}

# ── 1. The top-level exclusion must cover exactly the four documents ─────────
echo 'Checking the top-level pre-commit exclusion set (design §8.4)...'
if [ -f "$CONFIG" ]; then
	if ! grep -q '^exclude: |' "$CONFIG"; then
		fail 'no top-level "exclude: |" block in .pre-commit-config.yaml (design §8.4)'
	fi

	EXCLUDED=$(awk '
		/^exclude: \|/ { inblock = 1; next }
		inblock && /^[^ ]/ { inblock = 0 }
		inblock {
			line = $0
			sub(/^[ \t]+/, "", line)
			sub(/[ \t]+$/, "", line)
			if (line == "(?x)^(" || line == ")$" || line == "") next
			sub(/\|$/, "", line)
			gsub(/\\\./, ".", line)
			print line
		}
	' "$CONFIG")

	EXCLUDED_COUNT=$(printf '%s\n' "$EXCLUDED" | grep -c '[^[:space:]]')
	if [ "${EXCLUDED_COUNT:-0}" -ne 4 ]; then
		fail "the top-level exclusion must list exactly 4 entries, found ${EXCLUDED_COUNT:-0}"
	fi

	printf '%s\n' "$AUTHORITATIVE_DOCS" | {
		while IFS= read -r doc; do
			[ -n "$doc" ] || continue
			if ! printf '%s\n' "$EXCLUDED" | grep -qxF "$doc"; then
				fail "authoritative document missing from the top-level exclusion: $doc"
			fi
		done
	}

	printf '%s\n' "$EXCLUDED" | {
		while IFS= read -r entry; do
			[ -n "$entry" ] || continue
			if ! printf '%s\n' "$AUTHORITATIVE_DOCS" | grep -qxF "$entry"; then
				fail "unexpected entry in the top-level exclusion (exclusion set must be exactly the four documents): $entry"
			fi
		done
	}
fi

# ── 2. Gitleaks must remain unexcluded and must scan every file ──────────────
echo 'Checking that gitleaks is never excluded (design §8.4, §14.1)...'
if [ -f "$CONFIG" ]; then
	if ! hook_exists gitleaks; then
		fail 'the gitleaks hook is missing (design §14.1 two-gate secret scanning)'
	else
		if [ -n "$(hook_key gitleaks exclude)" ]; then
			fail 'gitleaks must not carry a per-hook exclude: a secret in a reference document is still a secret'
		fi
		if [ -n "$(hook_key gitleaks files)" ]; then
			fail 'gitleaks must not narrow its files: it scans all files (design §8.4)'
		fi
		if [ "$(hook_key gitleaks pass_filenames)" != 'false' ]; then
			fail 'gitleaks must set pass_filenames: false so the top-level exclusion cannot filter the four documents out of the scan'
		fi
		if [ "$(hook_key gitleaks always_run)" != 'true' ]; then
			fail 'gitleaks must set always_run: true so it is never skipped when only excluded documents are staged'
		fi
	fi
fi

# ── 3. The mutating hook set must exist and must honour the exclusion ────────
echo 'Checking the mutating hook set (design §8.4)...'
if [ -f "$CONFIG" ]; then
	printf '%s\n' "$MUTATING_HOOKS" | {
		while IFS= read -r hook; do
			[ -n "$hook" ] || continue
			if ! hook_exists "$hook"; then
				fail "required hook is missing: $hook (design §8.4)"
				continue
			fi
			if [ "$(hook_key "$hook" always_run)" = 'true' ]; then
				fail "$hook sets always_run: true, which bypasses the top-level four-document exclusion"
			fi
			if [ "$(hook_key "$hook" pass_filenames)" = 'false' ]; then
				fail "$hook sets pass_filenames: false, which bypasses the top-level four-document exclusion"
			fi
		done
	}

	# Only gitleaks may opt out of filename filtering.
	awk -F'\t' '
		$1=="hook" && ($3=="always_run" || $3=="pass_filenames") && $2!="gitleaks" {
			if (($3=="always_run" && $4=="true") || ($3=="pass_filenames" && $4=="false"))
				print $2 " (" $3 ": " $4 ")"
		}
	' "$FACTS" | {
		while IFS= read -r offender; do
			[ -n "$offender" ] || continue
			fail "only gitleaks may bypass filename filtering; offender: $offender"
		done
	}

	# Scoping (design §8.4): Ruff is backend-scoped, gofmt/go vet agent-scoped.
	for hook in ruff ruff-format; do
		files=$(hook_key "$hook" files)
		case "$files" in
		'^backend/'*) ;;
		*) fail "$hook must be scoped to backend/ (found files: '${files}')" ;;
		esac
	done
	for hook in gofmt go-vet; do
		files=$(hook_key "$hook" files)
		case "$files" in
		'^agent/'*) ;;
		*) fail "$hook must be scoped to agent/ (found files: '${files}')" ;;
		esac
	done

	# Prettier must reach frontend sources plus markdown/yaml (design §8.4).
	prettier_files=$(hook_key prettier files)
	case "$prettier_files" in
	*frontend*) ;;
	*) fail "prettier must cover frontend files (found files: '${prettier_files}')" ;;
	esac
	case "$prettier_files" in
	*md*) ;;
	*) fail "prettier must cover markdown files (found files: '${prettier_files}')" ;;
	esac
	case "$prettier_files" in
	*ya?ml* | *yaml* | *yml*) ;;
	*) fail "prettier must cover yaml files (found files: '${prettier_files}')" ;;
	esac
fi

# ── 4. Every hook repository revision must be pinned exactly ────────────────
echo 'Checking that hook repositories are pinned exactly (design §7.7)...'
if [ -f "$CONFIG" ]; then
	awk -F'\t' '$1=="repo" && $3=="rev" { print $2 "\t" $4 }' "$FACTS" | {
		while IFS="$(printf '\t')" read -r repo rev; do
			[ -n "$repo" ] || continue
			case "$rev" in
			'' | HEAD | main | master | stable | latest)
				fail "hook repository is not pinned to an exact revision: $repo -> '$rev'"
				;;
			v[0-9]*) ;;
			[0-9]*) ;;
			*)
				fail "hook repository revision is not an exact version tag: $repo -> '$rev'"
				;;
			esac
		done
	}

	# Every non-local repo needs a rev.
	awk -F'\t' '
		$1=="repo" && $3=="hook" && $2!="local" { seen[$2]=1 }
		$1=="repo" && $3=="rev" { pinned[$2]=1 }
		END { for (r in seen) if (!(r in pinned)) print r }
	' "$FACTS" | {
		while IFS= read -r repo; do
			[ -n "$repo" ] || continue
			fail "hook repository has no rev: $repo"
		done
	}

	deps=$(hook_key prettier additional_dependencies)
	case "$deps" in
	*@[0-9]*) ;;
	*) fail "prettier additional_dependencies must pin an exact version (found: '${deps}')" ;;
	esac
fi

# ── 5. .gitignore must ignore generated state and nothing tracked ───────────
echo 'Checking .gitignore coverage (design §0.3)...'
if [ -f "$IGNORE" ]; then
	printf '%s\n' "$REQUIRED_IGNORES" | {
		while IFS= read -r pattern; do
			[ -n "$pattern" ] || continue
			if ! grep -qxF "$pattern" "$IGNORE"; then
				fail "required .gitignore pattern is missing: $pattern"
			fi
		done
	}

	printf '%s\n' "$PROTECTED_PATHS" | {
		while IFS= read -r protected; do
			[ -n "$protected" ] || continue
			base=${protected##*/}
			while IFS= read -r line; do
				case "$line" in
				'' | '#'* | '!'*) continue ;;
				esac
				pat=${line%/}
				[ -n "$pat" ] || continue
				matched=0
				# shellcheck disable=SC2254
				case "$protected" in
				$pat) matched=1 ;;
				esac
				case "$pat" in
				*/*) ;;
				*)
					# shellcheck disable=SC2254
					case "$base" in
					$pat) matched=1 ;;
					esac
					;;
				esac
				if [ "$matched" -eq 1 ]; then
					fail "tracked input must never be ignored: '$protected' is matched by .gitignore pattern '$line'"
				fi
			done <"$IGNORE"
		done
	}
fi

VIOLATIONS=$(wc -l <"$FAILFILE" | tr -d ' \t')
if [ "${VIOLATIONS:-0}" -ne 0 ]; then
	printf '\nignore/pre-commit hygiene check failed with %s violation(s)\n' "$VIOLATIONS" >&2
	exit 1
fi

echo 'ignore/pre-commit hygiene check passed'
exit 0
