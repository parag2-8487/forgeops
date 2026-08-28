#!/usr/bin/env python
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Dump the real OpenAPI schema and render the endpoint reference from it.

WHY THIS EXISTS
`docs/api.md` was hand-written, and a hand-written endpoint table is a record with nothing holding
it to the code. It drifted: the surface grew from 33 paths to 45 across one working session while
the file sat unchanged, and nothing in CI could tell. The same is true of the SSE event vocabulary
-- `frontend/__tests__/sse-vocabulary.test.ts` needs a machine-readable copy of `SSEEventType` to
compare the browser's list against, and the only honest source for that is the schema the
application actually publishes.

So this boots `create_app()`, dumps `app.openapi()` to `docs/openapi.json`, and renders the
generated portion of `docs/api.md` between two markers. The hand-written part -- auth posture, the
RFC 9457 contract, the SSE vocabulary's meaning, pagination -- stays outside the markers and is
written by a person, because none of that is derivable from a schema.

    python scripts/dump-openapi.py            # write both files
    python scripts/dump-openapi.py --check    # fail if either is stale

`--check` is what CI runs, which is the whole point: a drifting record becomes a failing build
instead of a discovery months later.

The app is constructed with a committed-baseline environment and deliberately unreachable
infrastructure DSNs. Nothing here connects to anything: `app.openapi()` is pure reflection over the
route table, and requiring a database to render documentation would make the docs job depend on
services it has no business needing.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
DOCS = REPO_ROOT / "docs"
OPENAPI_PATH = DOCS / "openapi.json"
API_MD_PATH = DOCS / "api.md"

BEGIN_MARKER = "<!-- BEGIN GENERATED ENDPOINTS -->"
END_MARKER = "<!-- END GENERATED ENDPOINTS -->"

#: HTTP methods in the order a reader expects them, not the order a dict happens to yield.
METHOD_ORDER = ["get", "post", "put", "patch", "delete", "head", "options"]


#: The only environment the app is constructed under. Anything else is stripped.
#:
#: `setdefault` was not enough and the difference is the whole point of this gate. The `backend` CI
#: job sets DATABASE_URL, GENERATION_TIER, SELF_HOSTED_MODEL_ID and a dozen more at job level, so
#: `setdefault` left them in place and the schema rendered under CI's environment while a developer's
#: rendered under theirs. `--check` then reported `docs/openapi.json` stale in CI and current locally,
#: which is the same "fails there, passes here" shape that made the embedding-model constant a
#: standing environment note instead of a bug. A drift gate whose output depends on who runs it
#: cannot tell drift from difference, so the environment is now pinned rather than defaulted.
BASELINE_ENV = {
    "APP_ENV": "test",
    # Deliberately unreachable: rendering a schema must not be able to touch a database by accident.
    "DATABASE_URL": "postgresql+asyncpg://unused@127.0.0.1:1/unused",
    "REDIS_URL": "redis://127.0.0.1:1/0",
    # `Settings` refuses an empty ENVELOPE_PEPPER in every environment, because an empty one is not a
    # missing credential but a broken one. Without this the script could not construct the app at all
    # and `--check` raised a validation error instead of comparing anything -- and nothing noticed,
    # because until this pass `--check` ran nowhere despite this module's docstring saying it is what
    # CI runs. A fixed non-secret: this process renders a schema and signs nothing.
    "ENVELOPE_PEPPER": "openapi-dump-only-not-a-real-value",
}

#: Set in the isolated child so it does not re-exec itself forever.
_ISOLATION_SENTINEL = "FORGEOPS_OPENAPI_ISOLATED"

#: Kept from the ambient environment because the interpreter needs them to start at all.
_OS_PASSTHROUGH = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
    "COMSPEC",
    "PATHEXT",
)


def reexec_isolated() -> int:
    """Re-run this script with only `BASELINE_ENV` plus what the OS needs, and return its status."""
    env = {name: os.environ[name] for name in _OS_PASSTHROUGH if name in os.environ}
    env.update(BASELINE_ENV)
    env[_ISOLATION_SENTINEL] = "1"
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]],
        env=env,
        check=False,
    )
    return completed.returncode


def build_app() -> Any:
    """Construct the real application, with unreachable infrastructure on purpose."""
    sys.path.insert(0, str(BACKEND))
    os.environ.update(BASELINE_ENV)

    from src.main import create_app  # noqa: PLC0415 — after sys.path is arranged

    return create_app()


def render_endpoint_table(schema: dict[str, Any]) -> str:
    """One table per tag, because a single 45-row table is a list rather than a reference."""
    # Group by the first tag. FastAPI puts the router's tag on every operation, so this reproduces
    # the router structure without restating it here and going stale when a router is added.
    by_tag: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            if method.lower() not in METHOD_ORDER:
                continue
            tags = operation.get("tags") or ["untagged"]
            by_tag.setdefault(tags[0], []).append((path, method.lower(), operation))

    total_paths = len(schema.get("paths", {}))
    total_ops = sum(len(v) for v in by_tag.values())

    lines: list[str] = [
        BEGIN_MARKER,
        "",
        "<!-- Generated by scripts/dump-openapi.py from the live app. Do not edit by hand:",
        "     `python scripts/dump-openapi.py --check` fails the build when this drifts. -->",
        "",
        f"The application publishes **{total_ops} operations across {total_paths} paths**.",
        "",
    ]

    for tag in sorted(by_tag):
        lines.append(f"### `{tag}`")
        lines.append("")
        lines.append("| Method | Path | Summary | Auth |")
        lines.append("| --- | --- | --- | --- |")
        rows = sorted(by_tag[tag], key=lambda r: (r[0], METHOD_ORDER.index(r[1])))
        for path, method, operation in rows:
            summary = (operation.get("summary") or "").replace("|", "\\|")
            # Read from the schema rather than asserted here. `check-route-auth.py` is the arbiter
            # of the auth posture; this column only reports what the document says, so the two
            # cannot silently disagree about which routes are public.
            public = _is_public(path, method)
            auth = "public" if public else "principal"
            lines.append(f"| `{method.upper()}` | `{path}` | {summary} | {auth} |")
        lines.append("")

    lines.append(END_MARKER)
    return "\n".join(lines)


def _is_public(path: str, method: str) -> bool:
    """Ask the application's own public-route table, not a list restated here."""
    from src.auth.public_routes import is_public  # noqa: PLC0415

    return bool(is_public(path, method.upper()))


def splice(existing: str, generated: str) -> str:
    """Replace the generated block, preserving everything a person wrote around it."""
    if BEGIN_MARKER in existing and END_MARKER in existing:
        head = existing.split(BEGIN_MARKER)[0]
        tail = existing.split(END_MARKER, 1)[1]
        return head + generated + tail
    # First run: append rather than overwrite, so no hand-written prose is destroyed.
    return existing.rstrip() + "\n\n" + generated + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if either file would change",
    )
    args = parser.parse_args()

    # Re-exec once under a pinned environment, so the rendered schema is a function of the code alone.
    if not os.environ.get(_ISOLATION_SENTINEL):
        return reexec_isolated()

    app = build_app()
    schema = app.openapi()

    openapi_text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    generated = render_endpoint_table(schema)
    existing_md = API_MD_PATH.read_text(encoding="utf-8") if API_MD_PATH.exists() else "# API reference\n"
    api_md_text = splice(existing_md, generated)

    if args.check:
        stale: list[str] = []
        current_openapi = OPENAPI_PATH.read_text(encoding="utf-8") if OPENAPI_PATH.exists() else ""
        if current_openapi != openapi_text:
            stale.append(str(OPENAPI_PATH.relative_to(REPO_ROOT)))
        if existing_md != api_md_text:
            stale.append(str(API_MD_PATH.relative_to(REPO_ROOT)))
        if stale:
            print("dump-openapi: STALE: " + ", ".join(stale))
            print("dump-openapi: run `python scripts/dump-openapi.py` and commit the result")
            return 1
        paths = len(schema.get("paths", {}))
        print(f"dump-openapi: OK, docs match the live schema ({paths} paths)")
        return 0

    DOCS.mkdir(parents=True, exist_ok=True)
    OPENAPI_PATH.write_text(openapi_text, encoding="utf-8")
    API_MD_PATH.write_text(api_md_text, encoding="utf-8")
    print(f"dump-openapi: wrote {OPENAPI_PATH.relative_to(REPO_ROOT)} ({len(schema.get('paths', {}))} paths)")
    print(f"dump-openapi: wrote {API_MD_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
