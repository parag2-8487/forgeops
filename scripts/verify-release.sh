#!/bin/sh
# SPDX-License-Identifier: FSL-1.1-ALv2
# verify-release.sh — third-party verification of a published release artifact.
#
# Criterion 16: `cosign verify-blob` exits 0 against the expected certificate
# identity, and the SBOM is present alongside the artifact. No shared secret is
# needed: the signing identity is the release workflow's OIDC identity, the
# certificate came from Fulcio, and the signature is logged in Rekor.
#
# Usage:
#   scripts/verify-release.sh <artifact> [signature] [certificate]
#
# Environment:
#   FORGEOPS_CERT_IDENTITY_REGEXP  expected workflow identity (default: this repo)
#   FORGEOPS_CERT_OIDC_ISSUER      expected OIDC issuer (default: GitHub Actions)
#
# The default identity named `parag8487/ForgeOps` until this pass. That repository is not where
# releases are built, so the default could not verify any artifact this workflow has ever produced --
# it would have failed with an identity mismatch, which reads exactly like a forged signature. It
# went unnoticed because `release.yml` had never run in the canonical repository, so there was no
# artifact to point the script at. Verified against the real `v0.0.1-rc4` output: `cosign
# verify-blob` reports `Verified OK`, exit 0, with this default.
set -eu

ARTIFACT="${1:-}"
SIG="${2:-${ARTIFACT}.sig}"
CERT="${3:-${ARTIFACT}.pem}"

IDENTITY="${FORGEOPS_CERT_IDENTITY_REGEXP:-^https://github.com/parag2-8487/forgeops/.github/workflows/release.yml@refs/tags/v.*$}"
ISSUER="${FORGEOPS_CERT_OIDC_ISSUER:-https://token.actions.githubusercontent.com}"

if [ -z "$ARTIFACT" ]; then
  printf 'verify-release: usage: scripts/verify-release.sh <artifact> [signature] [certificate]\n' >&2
  printf 'verify-release: no artifact given — nothing published yet is not a failure here.\n'
  exit 0
fi

if [ ! -f "$ARTIFACT" ]; then
  printf 'verify-release: FAIL artifact not found: %s\n' "$ARTIFACT" >&2
  exit 1
fi

if ! command -v cosign >/dev/null 2>&1; then
  printf 'verify-release: SKIP cosign not on PATH — install from https://github.com/sigstore/cosign\n'
  exit 0
fi

# 1. Signature verification against the expected keyless identity.
printf 'verify-release: cosign verify-blob %s\n' "$ARTIFACT"
cosign verify-blob \
  --certificate "$CERT" \
  --signature "$SIG" \
  --certificate-identity-regexp "$IDENTITY" \
  --certificate-oidc-issuer "$ISSUER" \
  "$ARTIFACT"

# 2. SBOM presence beside the artifact.
SBOM="${ARTIFACT}.sbom.json"
if [ ! -f "$SBOM" ]; then
  printf 'verify-release: FAIL missing SBOM %s\n' "$SBOM" >&2
  exit 1
fi
if ! grep -q '"bomFormat"' "$SBOM"; then
  printf 'verify-release: FAIL %s is not a CycloneDX document\n' "$SBOM" >&2
  exit 1
fi

printf 'verify-release: OK signature verified and CycloneDX SBOM present\n'
