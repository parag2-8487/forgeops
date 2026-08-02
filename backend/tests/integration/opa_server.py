# SPDX-License-Identifier: FSL-1.1-ALv2
"""One definition of "a real OPA server loading a real bundle", for every test that needs one.

Extracted when leaf 9.2 became the second caller. `test_opa_policy_integration.py` already
had this logic for `policies/mcp`, and copying it for `policies/agent` would have been two
definitions of the same fixture — the shape `tests/property/conftest.py` and leaf 8.10 both
record as how a test comes to exercise a server the other tests never do. The digest, the
readiness poll, the port discovery and the teardown now live here.

Selection order, unchanged from the original:

1. `FORGEOPS_TEST_OPA_URL` when it names a healthy server (CI may set it);
2. otherwise a container started here from the digest-pinned image and torn down after;
3. otherwise `require_capability("opa")`, which **skips locally and fails in CI**, so a
   missing engine can never quietly remove criterion 7's only evidence (D-26).

Note that (1) is only usable by a caller whose bundle the preset server already loads. A
caller needing a *specific* bundle directory passes `preset_env=None` so the container path
is always taken; `governance_opa_url` does exactly that, because a server started for the
MCP bundle would answer the governance document as UNDEFINED and the failure would read
like a policy bug.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from .capability import require_capability

#: Same digest as docker-compose.yml, so every test runs on the engine the platform ships
#: with. §10.6.1's claim is that the agent, the server and the policy gate share one Rego
#: semantics; a test on a different OPA would be evidence about a different engine.
OPA_IMAGE = "openpolicyagent/opa:1.4.2@sha256:35a093d9ae828373cf88f68ecaa8189ab26287468074a3b78f0601d9c8b7a4f5"

#: Repository root, four parents up from `backend/tests/integration/opa_server.py`.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The two Rego bundles. Never `policies/` itself (D-57, D-91): `opa run /policies` loads
#: every YAML it finds as a DATA document, and task 6.4's six Cerbos files all declare
#: `apiVersion` at the top level, so OPA refuses to start with six merge errors.
MCP_POLICY_DIR = REPO_ROOT / "policies" / "mcp"
GOVERNANCE_POLICY_DIR = REPO_ROOT / "policies" / "agent"


def wait_until_healthy(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=1.0).status_code == 200:
                return True
        except Exception:
            time.sleep(0.3)
    return False


@contextmanager
def opa_server(policy_dir: Path, *, preset_env: str | None = "FORGEOPS_TEST_OPA_URL") -> Iterator[str]:
    """Yield the base URL of an OPA server loading `policy_dir`.

    Args:
        policy_dir: the Rego directory to mount. Must contain no YAML.
        preset_env: environment variable naming an already-running server, or `None` to
            insist on starting one. `None` is correct whenever the bundle matters, which is
            most of the time.
    """
    if preset_env:
        preset = os.environ.get(preset_env, "").strip()
        if preset and wait_until_healthy(preset, timeout=5.0):
            yield preset
            return

    docker = shutil.which("docker")
    if docker is None:
        require_capability("opa", f"no healthy {preset_env or 'preset'} and no docker on PATH to start one")

    name = f"forgeops-opa-test-{uuid.uuid4().hex[:8]}"
    started = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            docker,
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-p",
            "0:8181",
            "-v",
            f"{policy_dir.as_posix()}:/policies:ro",
            OPA_IMAGE,
            "run",
            "--server",
            "--addr=0.0.0.0:8181",
            "/policies",
        ],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        require_capability("opa", f"could not start the OPA container: {started.stderr.strip()[:200]}")

    try:
        port_line = subprocess.run(  # noqa: S603
            [docker, "port", name, "8181/tcp"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()[0]
        url = f"http://127.0.0.1:{port_line.rsplit(':', 1)[-1].strip()}"
        if not wait_until_healthy(url):
            require_capability("opa", "the OPA container never became healthy")
        yield url
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, check=False)  # noqa: S603
