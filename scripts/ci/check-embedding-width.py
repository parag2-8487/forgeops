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
from pathlib import Path

#: The width `analysis/models.py` declares for `embeddings_local.embedding` (D-48).
EXPECTED_DIMENSIONS = 1024

DEFAULT_BASE_URL = "http://localhost:11434/v1"


def _from_dotenv(name: str) -> str:
    """Read one key from `.env`, because that is where the value actually lives.

    The compose services get these through `env_file`, so `SELF_HOSTED_EMBEDDING_MODEL_ID` is set for
    the CONTAINERS and unset for the runner shell that invokes this script. The first CI run of this
    check failed on exactly that — the model server was healthy with both models pulled, and the check
    reported "SELF_HOSTED_EMBEDDING_MODEL_ID is unset".

    Sourcing the whole file with `set -a; . ./.env` was the obvious alternative and is wrong: the
    baseline carries INLINE COMMENTS (`ENVELOPE_PEPPER=change-me-locally  # HMAC pepper ...`), which a
    shell would fold into the value. So the key is parsed out, with the comment stripped, the same way
    a dotenv reader would.

    Returns the empty string when there is no `.env` or no such key, which the caller reports.
    """
    path = Path(os.environ.get("FORGEOPS_DOTENV", ".env"))
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != name:
            continue
        # An unquoted `#` starts a comment; a quoted value keeps it.
        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            return value[1:-1]
        return value.split("#", 1)[0].strip()
    return ""


def _setting(name: str, default: str = "") -> str:
    """The environment wins, then `.env`, then the default — the same precedence `Settings` uses."""
    return (os.environ.get(name) or _from_dotenv(name) or default).strip()


def main() -> int:
    # `.env` is consulted because that is where compose reads these from; see `_from_dotenv`.
    base_url = _setting("SELF_HOSTED_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = _setting("SELF_HOSTED_EMBEDDING_MODEL_ID")
    if not model:
        print(
            "check-embedding-width: SELF_HOSTED_EMBEDDING_MODEL_ID is unset, so there is nothing to "
            "check. Set it to a 1024-d model; see .env.example.",
            file=sys.stderr,
        )
        return 1

    # `.env` names the model server by its COMPOSE hostname (`http://ollama:11434/v1`), which does not
    # resolve from the runner. `SELF_HOSTED_BASE_URL` in the environment overrides it when set; when the
    # value came from `.env` and names a host this process cannot reach, fall back to the published
    # port. Guessing silently would be wrong, so the substitution is stated.
    if "//ollama:" in base_url and not os.environ.get("SELF_HOSTED_BASE_URL"):
        print(
            f"check-embedding-width: {base_url} is a compose hostname; using {DEFAULT_BASE_URL}"
        )
        base_url = DEFAULT_BASE_URL.rstrip("/")

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
    if (
        not data
        or not isinstance(data[0], dict)
        or not isinstance(data[0].get("embedding"), list)
    ):
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

    print(
        f"check-embedding-width: {model} returns {width} dimensions, as embeddings_local requires"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
