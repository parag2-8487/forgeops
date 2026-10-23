#!/usr/bin/env bash
# scripts/package-agent.sh - produce the agent's release archives from the working tree.
#
# WHY THIS EXISTS, and why it is not a second build path.
#
# The onboarding screen prints an install command that looks for a RELEASE ARCHIVE
# (`forgeops-agent_*_<os>_*.tar.gz` in the current folder or `~/Downloads`), extracts it and installs the
# binary from inside it. Two things were therefore untestable without a published release, and both were
# permanently red:
#
#   * `printed-instructions.spec.ts` built a bare binary with `go build` and then ran the printed command
#     verbatim. The command did exactly what it promises — found no tarball, said so, and exited 0 — so
#     nothing was installed and `forgeops-agent version` reported `command not found`. The spec's own
#     setup, not the product, was what did not match the instructions.
#   * `ci / agent (windows-latest host binary)` downloaded a published release archive. The mirror has no
#     releases, so that job could never pass on any commit, and a permanently red job teaches everyone to
#     ignore red.
#
# Both now install the archive THIS script produces, and it produces it by invoking GoReleaser against the
# same `agent/.goreleaser.yaml` the tagged release uses. That matters more than the convenience: if the
# archive layout, the binary name inside it or the file-name template ever drifts from what the UI prints,
# the drift fails these checks instead of reaching a user who followed the instructions and got nothing.
# Writing a `tar czf` here would have been three lines and would have tested the three lines.
#
# Signing, SBOMs, publishing and announcements are skipped: those are properties of a RELEASE, they need
# credentials and a tag, and the release workflow still performs and self-verifies every one of them. The
# build, the archive shape and the file names are what this asserts, and they are shared.
#
# Usage: scripts/package-agent.sh [<dest-dir>]
#   With no argument the archives are left in `agent/dist`. With one, the archive and checksum file for the
#   HOST platform are copied there, which is what a caller that then runs the printed command wants.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AGENT_DIR="$REPO_ROOT/agent"
DEST="${1:-}"

if ! command -v goreleaser >/dev/null 2>&1; then
  echo "package-agent: goreleaser is not on PATH." >&2
  echo "package-agent: CI installs it with the pinned goreleaser-action step (v2.17.1) that ci.yml and" >&2
  echo "package-agent: e2e-ci.yml already use; locally, install that same version. Deliberately not" >&2
  echo "package-agent: suggesting a floating install here: the archive layout is what this asserts, so" >&2
  echo "package-agent: the tool that produces it has to be the pinned one." >&2
  exit 1
fi

# `--snapshot` because there is no tag: it derives a version from the commit rather than refusing. `--clean`
# so a stale archive from an earlier commit can never be the one a caller installs — that failure mode looks
# exactly like a successful install of the wrong bytes.
(
  cd "$AGENT_DIR"
  goreleaser release --snapshot --clean --skip=publish,sign,sbom,announce
)

if [ -n "$DEST" ]; then
  mkdir -p "$DEST"

  # The HOST platform, resolved from the Go toolchain rather than from `uname`, because the archive names
  # come from GoReleaser's `{{ .Os }}_{{ .Arch }}` and those are Go's spellings. `uname -m` says `x86_64`
  # where Go says `amd64`, and a mismatch here would silently copy nothing.
  GOOS="$(cd "$AGENT_DIR" && go env GOOS)"
  GOARCH="$(cd "$AGENT_DIR" && go env GOARCH)"

  found=0
  for f in "$AGENT_DIR"/dist/*_"${GOOS}"_"${GOARCH}".tar.gz "$AGENT_DIR"/dist/*_"${GOOS}"_"${GOARCH}".zip; do
    [ -e "$f" ] || continue
    cp "$f" "$DEST/"
    found=1
  done

  if [ "$found" -eq 0 ]; then
    echo "package-agent: no archive was produced for ${GOOS}/${GOARCH}." >&2
    echo "package-agent: what dist holds:" >&2
    ls -1 "$AGENT_DIR/dist" >&2 || true
    exit 1
  fi

  echo "package-agent: archives for ${GOOS}/${GOARCH} are in $DEST"
  ls -1 "$DEST"
fi
