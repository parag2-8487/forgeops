#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Write the demo project fixture onto disk, so a real cycle can be run against it.

The fixture is `demo_project_before()` in `backend/tests/unit/test_reachable_score_contract.py`, which
is where its shape is argued for: it supplies the five things generation legitimately cannot produce
and withholds the Kubernetes manifests and the compose file. That module is the single definition, and
this script loads it rather than restating it — a second copy would drift, and a drifted copy of a
90-point fixture is a 90 that means nothing.

Run from `backend/` with its virtualenv, because the module imports `src`:

    backend/.venv/Scripts/python ../scripts/ci/materialise-demo-project.py <target-directory>

Bytes are written with LF endings and no BOM. Both matter: `change_items.old_hash` is the SHA-256 of
what an apply is about to overwrite, so a stray CRLF makes the stale-apply guard abort the set.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

FIXTURE_MODULE = pathlib.Path("tests/unit/test_reachable_score_contract.py")


def load_fixture() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("demo_fixture", FIXTURE_MODULE)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {FIXTURE_MODULE}; run this from backend/")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(module.demo_project_before())


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit(__doc__)
    root = pathlib.Path(argv[0])
    files = load_fixture()
    for relative, body in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # `newline=""` on the handle, so the LF in the fixture is the LF on disk on every platform.
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(body)
    print(json.dumps({"root": str(root), "files": sorted(files)}))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, ".")
    raise SystemExit(main(sys.argv[1:]))
