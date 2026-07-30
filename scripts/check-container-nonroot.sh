#!/usr/bin/env bash
# SPDX-License-Identifier: FSL-1.1-ALv2
#
# Assert that a running Compose service's process is not uid 0.
#
# design.md §0.5 debt D5, as corrected by §17.1 D-51.
#
# D5 asked for `openpolicyagent/opa:1.4.2-rootless`. That tag does not exist — OPA
# 1.x publishes `1.4.2`, `-static`, `-debug`, `-envoy*` and `-istio*`, and the
# `-rootless` suffix was retired with the 0.x line — and the pinned `1.4.2` image
# already runs as `1000:1000` on a Chainguard base, so the security intent was
# already met. More importantly, matching a suffix in a tag name proves a naming
# convention, not a runtime user: a `-rootless` image handed `user: root` in Compose
# would have passed such a gate while running as root.
#
# `scripts/check-compose-validate.py` rejects a root `user:` override statically.
# This is the other half, and it is deliberately not `docker compose exec ... id -u`:
# the OPA image is distroless and contains neither `id` nor a shell, so `exec` cannot
# run anything at all. `docker top` reads the host process table instead, which is the
# uid the kernel actually applied — a stronger observation than the container config,
# and one that needs nothing inside the image.
#
# Usage: check-container-nonroot.sh <compose-service> [<compose-service> ...]
set -euo pipefail

cd "$(dirname "$0")/.."

if [ "$#" -eq 0 ]; then
  echo "Usage: $(basename "$0") <compose-service> [<compose-service> ...]" >&2
  exit 1
fi

status=0

for service in "$@"; do
  cid="$(docker compose ps -q "$service" || true)"
  if [ -z "$cid" ]; then
    echo "FAIL: service '${service}' has no running container; nothing was proved" >&2
    status=1
    continue
  fi

  # Column 1 of `docker top` is UID; row 1 is the header, so the first process is
  # row 2. An image with several processes is fine: every row must be non-root.
  uids="$(docker top "$cid" | tail -n +2 | tr -s ' ' | cut -d' ' -f1 | grep -v '^$' || true)"
  if [ -z "$uids" ]; then
    echo "FAIL: service '${service}' reported no processes; nothing was proved" >&2
    status=1
    continue
  fi

  while read -r uid; do
    if [ "$uid" = "0" ] || [ "$uid" = "root" ]; then
      echo "FAIL: service '${service}' runs a process as uid ${uid} (root)" >&2
      status=1
    else
      echo "OK: service '${service}' process runs as uid ${uid}"
    fi
  done <<EOF
$uids
EOF
done

exit "$status"
