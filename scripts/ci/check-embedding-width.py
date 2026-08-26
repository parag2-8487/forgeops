#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Assert the local embedding model returns the width `embeddings_local` is declared at.

WHY THIS RUNS BEFORE THE TESTS RATHER THAN BEING LEFT TO THEM

`embeddings_local.embedding` is `vector(1024)` because D-48 sizes it for BGE-M3. A server configured
with a different embedding model — `nomic-embed-text` is 768 and was the previous default — fails
inside the INSERT, per row, with an error that names neither the model nor the setting that chose it,
AFTER every vector has already been computed and paid for.

`SelfHostedChunkEmbedder` refuses a wrong width by name at runtime, which is the right behaviour for
a deployment. In CI the useful place to find out is before an hour of tests, so this checks the same
thing at the point the model server is declared ready.

A COMMITTED SCRIPT RATHER THAN AN INLINE HEREDOC. The first version of this lived in a
`run: |` block as `python - <<'PY'`, which puts Python's indentation inside YAML's and gives a file
where a whitespace mistake is a workflow that parses and misbehaves. `scripts/ci/print-development-ca.py`
established the pattern; this follows it, and it can be run by hand when a developer's stack looks
wrong.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

#: The width `analysis/models.py` declares for `embeddings_local.embedding` (D-48).
EXPECTED_DIMENSIONS = 1024

DEFAULT_BASE_URL = "http://localhost:11434/v1"


def main() -> int:
    base_url = os.environ.get("SELF_HOSTED_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("SELF_HOSTED_EMBEDDING_MODEL_ID", "").strip()
    if not model:
        print(
            "check-embedding-width: SELF_HOSTED_EMBEDDING_MODEL_ID is unset, so there is nothing to "
            "check. Set it to a 1024-d model; see .env.example.",
            file=sys.stderr,
        )
        return 1

    request = urllib.request.Request(  # noqa: S310 - a fixed http(s) URL from configuration
        f"{base_url}/embeddings",
        data=json.dumps({"input": "probe", "model": model}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310
            payload = json.load(response)
    except urllib.error.URLError as exc:
        print(
            f"check-embedding-width: {base_url} did not answer an embedding request for {model!r}: "
            f"{exc}. The model server is not reachable, so the self-hosted tier has nothing to route "
            f"to and generation would silently fall back to templates.",
            file=sys.stderr,
        )
        return 1

    data = payload.get("data") or []
    if not data or not isinstance(data[0], dict) or not isinstance(data[0].get("embedding"), list):
        print(
            f"check-embedding-width: {model!r} returned no vector. Response keys: {sorted(payload)}",
            file=sys.stderr,
        )
        return 1

    width = len(data[0]["embedding"])
    if width != EXPECTED_DIMENSIONS:
        print(
            f"check-embedding-width: {model!r} returns {width} dimensions, and "
            f"embeddings_local.embedding is vector({EXPECTED_DIMENSIONS}). D-48 sizes that column for "
            f"BGE-M3; set SELF_HOSTED_EMBEDDING_MODEL_ID to a {EXPECTED_DIMENSIONS}-d model.",
            file=sys.stderr,
        )
        return 1

    print(f"check-embedding-width: {model} returns {width} dimensions, as embeddings_local requires")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
