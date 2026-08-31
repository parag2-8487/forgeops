# SPDX-License-Identifier: FSL-1.1-ALv2
"""The agent's embedded OPA and the backend's OPA must reach the same verdict (FR-38).

§1.10's whole claim is that the two sides agree, and `policy_evaluations.side` exists so a disagreement
becomes a row you can query for rather than an invisible bug. What was never checked is the claim itself.

The two evaluators are genuinely different pieces of software:

* the **backend** POSTs an input document to a running `openpolicyagent/opa` server over HTTP and reads
  `/v1/data/forgeops/governance/decision`;
* the **agent** links OPA's Go `rego` package and evaluates the same bundle in process, with no network
  and no server.

So "they agree" is a real property with a real way to fail — a version skew between the server image and
the Go module, a bundle that loads differently through `bundle.NewReader` than through the server's
loader, a builtin available in one and not the other. This file asks both the same questions and compares
the answers.

The agent side is driven through `agent/cmd/evalhelper`, which exists for exactly this: a tiny binary that
loads a bundle, evaluates one input, and prints the decision as JSON. Using the real helper rather than
reimplementing the call means the code under test is the code that ships.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_DIR = REPO_ROOT / "agent"
BUNDLE_DIR = REPO_ROOT / "policies" / "agent"

#: 2026-08-07T02:30:00Z is a Friday in UTC and Friday 08:00 in Asia/Kolkata — the instant
#: `governance_test.rego` and `test_governance_policy_opa.py` both use, so three suites cannot drift into
#: asserting different calendars.
FRIDAY_0230_UTC = "2026-08-07T02:30:00Z"
WEDNESDAY_1000_UTC = "2026-08-05T10:00:00Z"


def _require(tool: str, why: str) -> None:
    if shutil.which(tool) is not None:
        return
    if os.environ.get("FORGEOPS_REQUIRE_INTEGRATION", "").strip():
        pytest.fail(f"FORGEOPS_REQUIRE_INTEGRATION is set but `{tool}` is not on PATH: {why}")
    pytest.skip(f"`{tool}` is not on PATH: {why}")


#: The inputs both engines are asked about. Chosen to cover an allow, both refusal rules, and the
#: approval path, because "they agree" over allows alone is not evidence.
CASES: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "a plain allow",
        {
            "operation": "changeset.apply",
            "now_rfc3339": WEDNESDAY_1000_UTC,
            "environment": "dev",
            "change_items": [{"path": "src/index.ts", "action": "modify"}],
            "project": {},
        },
    ),
    (
        "a blocked weekday",
        {
            "operation": "changeset.apply",
            "now_rfc3339": FRIDAY_0230_UTC,
            "environment": "dev",
            "change_items": [{"path": "src/index.ts", "action": "modify"}],
            "project": {"timezone": "Asia/Kolkata", "blocked_weekdays": ["Friday"]},
        },
    ),
    (
        "a protected path",
        {
            "operation": "changeset.apply",
            "now_rfc3339": WEDNESDAY_1000_UTC,
            "environment": "dev",
            "change_items": [{"path": "services/web/package.json", "action": "modify"}],
            "project": {"protected_globs": ["**/package.json"]},
        },
    ),
    (
        "a production environment",
        {
            "operation": "changeset.apply",
            "now_rfc3339": WEDNESDAY_1000_UTC,
            "environment": "prod",
            "change_items": [{"path": "src/index.ts", "action": "modify"}],
            "project": {},
        },
    ),
    (
        "no change items at all",
        {
            "operation": "scan.full",
            "now_rfc3339": WEDNESDAY_1000_UTC,
            "environment": "dev",
            "change_items": [],
            "project": {"protected_globs": ["**/package.json"]},
        },
    ),
)


def _opa_cli_decision(document: dict[str, Any], tmp_path: Path) -> str:
    """The BACKEND's engine, asked through the same `opa` binary the backend image carries.

    `opa eval` over the real bundle directory rather than the HTTP server, because the server needs a
    container and the question here is whether the RULES agree, not whether two transports do. The
    server path is covered by `test_governance_policy_opa.py`, which runs against a real
    digest-pinned `openpolicyagent/opa`.
    """
    payload = tmp_path / "input.json"
    payload.write_bytes(json.dumps(document).encode("utf-8"))
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "opa",
            "eval",
            "--format",
            "json",
            "--data",
            str(BUNDLE_DIR),
            "--input",
            str(payload),
            "data.forgeops.governance.decision",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if "rego_" in completed.stderr:
        pytest.fail(f"the bundle failed to load in the opa CLI:\n{completed.stderr}")
    parsed = json.loads(completed.stdout)
    value = parsed["result"][0]["expressions"][0]["value"]
    return str(value["result"])


def _agent_decision(document: dict[str, Any], bundle_tarball: Path) -> str:
    """The AGENT's engine: OPA's Go rego package, in process, via `cmd/evalhelper`.

    The real helper rather than a reimplementation, so the code under test is the code that ships. It
    takes a built bundle archive and the input as an inline JSON string, and prints `{"decision": {...}}`.
    """
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "go",
            "run",
            "./cmd/evalhelper",
            "-bundle",
            str(bundle_tarball),
            "-input",
            json.dumps(document),
        ],
        cwd=AGENT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"the agent evaluator failed:\n{completed.stdout}\n{completed.stderr}")
    try:
        printed = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:  # pragma: no cover - defensive
        pytest.fail(f"the agent evaluator printed no decision: {completed.stdout!r} ({exc})")
    if "error" in printed:
        pytest.fail(f"the agent evaluator reported: {printed['error']}")
    return str(printed["decision"]["result"])


@pytest.fixture(scope="module")
def bundle_tarball(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the shipped bundle with `opa build`, which is how the agent receives one in production.

    Module-scoped: the archive is identical for every case, and building it per case would make five
    comparisons cost five builds.
    """
    _require("opa", "the bundle has to be built before the agent can load it")
    destination = tmp_path_factory.mktemp("bundle") / "bundle.tar.gz"
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["opa", "build", "-o", str(destination), str(BUNDLE_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"opa build failed:\n{completed.stdout}{completed.stderr}")
    return destination


@pytest.mark.parametrize(("name", "document"), CASES, ids=[case[0] for case in CASES])
def test_both_engines_reach_the_same_verdict(
    name: str, document: dict[str, Any], tmp_path: Path, bundle_tarball: Path
) -> None:
    """§1.10's claim, asked of both engines over identical input."""
    _require("opa", "the backend's engine")
    _require("go", "the agent's engine is compiled and run from source")

    backend = _opa_cli_decision(document, tmp_path)
    agent = _agent_decision(document, bundle_tarball)
    assert backend == agent, (
        f"the two engines disagree on {name!r}: backend={backend!r} agent={agent!r}. "
        "§1.10's claim is that they agree; a skew between the OPA server image and the Go rego module "
        "would look exactly like this."
    )


def test_the_cases_cover_more_than_allows(tmp_path: Path) -> None:
    """A test that only ever compares two allows is not evidence of agreement."""
    _require("opa", "the backend's engine")
    verdicts = {_opa_cli_decision(document, tmp_path) for _, document in CASES}
    assert "allow" in verdicts, verdicts
    assert "deny" in verdicts, f"no case produces a deny, so agreement on refusals is untested: {verdicts}"
    assert "require_approval" in verdicts, f"no case requires approval: {verdicts}"


def test_the_bundle_under_test_is_the_shipped_one() -> None:
    """Both engines must be reading `policies/agent`, not a fixture."""
    assert BUNDLE_DIR.is_dir(), BUNDLE_DIR
    rego_files = sorted(p.name for p in BUNDLE_DIR.glob("*.rego") if not p.name.endswith("_test.rego"))
    assert rego_files, "no policy files found, so both engines would agree vacuously"
    # Named individually: if a rule file is added and this list is not updated, the new rule is being
    # compared by accident rather than deliberately.
    assert {"governance.rego", "paths.rego", "schedule.rego", "approval.rego"} <= set(rego_files), rego_files


def test_the_agent_helper_exists_where_this_file_expects_it() -> None:
    """A path that has moved would make every comparison above skip rather than fail."""
    assert (AGENT_DIR / "cmd" / "evalhelper").is_dir(), "cmd/evalhelper has moved"
    assert sys.version_info >= (3, 11)
