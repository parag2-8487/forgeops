#!/bin/sh
# SPDX-License-Identifier: FSL-1.1-ALv2
# sbom.sh — generate a CycloneDX JSON SBOM for the agent (criterion 15).
#
# Syft reads agent/go.mod, so the SBOM reports the agent's Apache-2.0 licence
# and its dependency graph. Output goes to agent/dist/ which `make clean` removes.
set -eu

OUT_DIR="agent/dist"
OUT="$OUT_DIR/forgeops-agent.sbom.json"

if ! command -v syft >/dev/null 2>&1; then
  printf 'sbom: SKIP syft not on PATH — install from https://github.com/anchore/syft\n'
  exit 0
fi

mkdir -p "$OUT_DIR"
printf 'sbom: syft agent/ -o cyclonedx-json\n'
syft "dir:agent" -o cyclonedx-json > "$OUT"

# Validate the shape: a CycloneDX document declares bomFormat and components.
if ! grep -q '"bomFormat"' "$OUT"; then
  printf 'sbom: FAIL output is not a CycloneDX document\n' >&2
  exit 1
fi

# The agent embeds tree-sitter grammars as Wasm blobs. Syft reads `agent/go.mod` and cannot see
# them, so the document above describes the agent's Go dependency graph and silently omits a set of
# third-party components that ship inside the binary. `sbom-merge.py` adds them from
# `grammars.lock.json`, which is the file that pins their versions and digests.
#
# It existed for this and was called from nowhere -- not this script, not release.yml, not the
# Makefile -- so every SBOM produced so far has been incomplete in a way that only a reader who
# already knew about the grammars could detect.
printf 'sbom: merging tree-sitter grammar components\n'
python scripts/sbom-merge.py "$OUT" agent/internal/scanner/grammars/grammars.lock.json

if ! grep -q '"bomFormat"' "$OUT"; then
  printf 'sbom: FAIL merge left the output invalid\n' >&2
  exit 1
fi

printf 'sbom: OK wrote %s\n' "$OUT"
