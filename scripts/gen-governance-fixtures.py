# SPDX-License-Identifier: FSL-1.1-ALv2
"""Generate the Q-06 cross-runtime governance fixture corpus.

Design Appendix B states Q-06 as:

    ∀ governance inputs (operations × change-item sets × weekdays × timezones × verdicts ×
    environments), when the bundle digests are equal: the backend OPA-server decision equals
    the agent's embedded decision.

and names the library as "hypothesis (cross-runtime via a fixture corpus)".

WHY A CORPUS AND NOT ONE TEST
-----------------------------
The two evaluators cannot both be called from one process. The backend asks an OPA SERVER over
HTTP after mapping its stage-1 payload through `src.policies.opa.governance_input`; the agent
evaluates the same bundle in-process through OPA's Go Rego library. So the agreement is recorded
the same way `Q-14`'s is: this script drives the PYTHON side and commits the inputs together with
the decisions OPA returned, and `agent/internal/policy/q06_property_test.go` re-derives the agent
side from the committed inputs and compares.

That asymmetry is what makes it a two-way lock. Break the Python mapping and this script's output
changes, so the Go test fails against the committed bytes. Break the agent's evaluation and the Go
test fails directly. Regenerate after breaking Python and the Go test fails, which is the case the
arrangement exists to catch.

THE INPUTS ARE ENUMERATED, NOT SAMPLED
--------------------------------------
Appendix B lists the axes, so the corpus is their cross product rather than a random draw: a
committed fixture that changed between runs would make every regression look like a diff. The
generated set is deliberately small enough to read and large enough to cover each axis at least
twice, and `q06_property_test.go` carries a floor constant that may only be raised — the same
mechanism `corpus_test.go` uses, because a glob or a loop that matched nothing would make every
assertion pass over an empty slice.

Usage:
    # with an OPA server holding policies/agent on 127.0.0.1:8182
    python scripts/gen-governance-fixtures.py --opa-url http://127.0.0.1:8182
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = REPO_ROOT / "backend"
CORPUS = REPO_ROOT / "agent" / "testdata" / "governance" / "q06_corpus.json"

DECISION_PATH = "/v1/data/forgeops/governance/decision"

#: The axes Appendix B names.
OPERATIONS = ("apply", "revert", "deploy", "restart")
ENVIRONMENTS = (None, "dev", "staging", "prod")
#: A Friday inside the blocked window, a Friday outside it, and a Tuesday, each in two zones.
INSTANTS = (
    "2026-08-21T15:30:00Z",  # Friday, inside a typical blocked window
    "2026-08-21T02:00:00Z",  # Friday, outside it
    "2026-08-18T11:00:00Z",  # Tuesday
    "2026-08-21T15:30:00+05:30",  # same Friday instant, offset zone
)
CHANGE_ITEM_SETS: tuple[tuple[dict[str, Any], ...], ...] = (
    (),
    ({"path": "src/app.py", "action": "modify"},),
    ({"path": "package.json", "action": "modify"},),
    (
        {"path": "src/app.py", "action": "modify"},
        {"path": "package.json", "action": "delete"},
    ),
)
#: Appendix B's "verdicts" axis: the Semantic Plan Analyzer's blast-radius verdict, which is
#: absent at stage 1 and present later. Both shapes are covered because `approval.rego` treats
#: them differently and finding 71 was exactly that distinction.
BLAST_RADII: tuple[Any, ...] = (None, {"verdict": "ALLOW"}, {"verdict": "BLOCK"}, {})


def build_payloads() -> list[dict[str, Any]]:
    """The chokepoint-shaped payloads, before mapping."""
    payloads: list[dict[str, Any]] = []
    for operation, environment, instant in itertools.product(OPERATIONS, ENVIRONMENTS, INSTANTS):
        # Rotate the remaining two axes rather than multiplying them in, so the corpus stays
        # readable while every value of each still appears many times.
        items = CHANGE_ITEM_SETS[len(payloads) % len(CHANGE_ITEM_SETS)]
        blast = BLAST_RADII[len(payloads) % len(BLAST_RADII)]
        payload: dict[str, Any] = {
            "operation": operation,
            "now": instant,
            "items": list(items),
            "policy_parameters": {
                "timezone": "Europe/London",
                "blocked_weekdays": ["friday"],
                "blocked_operations": ["deploy"],
                "blocked_window": {"start": "12:00", "end": "23:59"},
                "protected_globs": ["package.json"],
            },
            "project_id": "11111111-1111-1111-1111-111111111111",
            "tenant_id": "22222222-2222-2222-2222-222222222222",
            "device_id": "33333333-3333-3333-3333-333333333333",
            "bundle_digest": "d" * 64,
        }
        if environment is not None:
            payload["environment"] = environment
        if blast is not None:
            payload["blast_radius"] = blast
        payloads.append(payload)
    return payloads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opa-url", default="http://127.0.0.1:8182")
    args = parser.parse_args(argv)

    # `governance_input` is the mapping under test; import it from the backend package.
    sys.path.insert(0, str(BACKEND_ROOT))
    from src.policies.opa import governance_input  # noqa: E402

    payloads = build_payloads()
    client = httpx.Client(base_url=args.opa_url, timeout=10.0)

    fixtures = []
    for payload in payloads:
        document = governance_input(payload)
        response = client.post(DECISION_PATH, json={"input": document})
        response.raise_for_status()
        body = response.json()
        if "result" not in body:
            raise SystemExit(
                f"OPA answered an UNDEFINED document for {document!r}. The bundle is not loaded; "
                "a corpus recorded against an undefined document would assert nothing."
            )
        fixtures.append({"input": document, "decision": body["result"]})

    # A corpus in which every decision is identical proves nothing about the axes, so the
    # generator refuses to write one. This is the same non-vacuity guard the rest of the suite
    # uses, applied to the fixture data itself.
    distinct = {json.dumps(f["decision"], sort_keys=True) for f in fixtures}
    if len(distinct) < 2:
        raise SystemExit(
            f"every one of the {len(fixtures)} inputs produced the same decision "
            f"({distinct}). The corpus would not distinguish an agreeing agent from a "
            "constant one."
        )

    CORPUS.parent.mkdir(parents=True, exist_ok=True)
    CORPUS.write_text(
        json.dumps(
            {
                "generated_by": "scripts/gen-governance-fixtures.py",
                "property": "Q-06",
                "decision_path": "data.forgeops.governance.decision",
                "fixtures": fixtures,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {CORPUS.relative_to(REPO_ROOT).as_posix()}: {len(fixtures)} fixtures")
    print(f"distinct decisions: {len(distinct)}")
    for decision in sorted(distinct):
        count = sum(1 for f in fixtures if json.dumps(f["decision"], sort_keys=True) == decision)
        print(f"  {count:3d}  {decision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
