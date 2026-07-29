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

printf 'sbom: OK wrote %s\n' "$OUT"
