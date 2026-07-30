# SPDX-License-Identifier: FSL-1.1-ALv2
"""The REAL OpaGatewayPolicy against a REAL OPA server loading the REAL policy.

Why this file exists
--------------------
`policies/mcp/gateway.rego` passed 27/27 of its own Rego tests while the backend
queried `/v1/data/forgeops/mcp/filter_tools` and `/allow_call` — paths that do not
exist in `package mcp.gateway`. OPA answers an undefined document with HTTP 200 and
a body with no `result` key, so `raise_for_status()` never fired: every
`tools/list` came back empty and every `tools/call` returned 403. Rego unit tests
cannot see that, because they never go through the HTTP client, and the Python
tests could not see it either, because they stubbed the policy entirely.

This test closes the gap end to end: a real `opa run --server` process loading the
committed policy directory, driven by the real `OpaGatewayPolicy`.

The server is provided by the digest-pinned OPA image already used by
`docker-compose.yml`, started for the duration of the module and torn down after.
It is selected via `FORGEOPS_TEST_OPA_URL` when an OPA is already running (CI sets
this), otherwise the module starts a container itself, and only skips when neither
an OPA nor a Docker daemon is available.

Design authority: §11.4, §5.4, §14.1 (fail-closed policy).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
from src.mcp.policy import DEFAULT_ALLOW_PATH, DEFAULT_FILTER_PATH, OpaGatewayPolicy

from .capability import require_capability

# Same digest as docker-compose.yml, so the policy runs on the engine it ships with.
OPA_IMAGE = "openpolicyagent/opa:1.4.2@sha256:35a093d9ae828373cf88f68ecaa8189ab26287468074a3b78f0601d9c8b7a4f5"
POLICY_DIR = Path(__file__).resolve().parents[3] / "policies"

CLAIMS = {"sub": "user-42"}

READ_ONLY_TOOL = {"name": "agent.health", "annotations": {"blast_radius": "read_only"}}
WORKSPACE_TOOL = {"name": "agent.file.write", "annotations": {"blast_radius": "workspace"}}
INFRA_TOOL = {"name": "agent.tofu.apply", "annotations": {"blast_radius": "infrastructure"}}
UNANNOTATED_TOOL = {"name": "agent.mystery"}  # no blast_radius at all

ALL_TOOLS = [READ_ONLY_TOOL, WORKSPACE_TOOL, INFRA_TOOL, UNANNOTATED_TOOL]


def _wait_until_healthy(url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/health", timeout=1.0).status_code == 200:
                return True
        except Exception:
            time.sleep(0.3)
    return False


@pytest.fixture(scope="module")
def opa_url() -> str:
    """A reachable OPA server loading `policies/`, started here if necessary."""
    preset = os.environ.get("FORGEOPS_TEST_OPA_URL", "").strip()
    if preset and _wait_until_healthy(preset, timeout=5.0):
        yield preset
        return

    docker = shutil.which("docker")
    if docker is None:
        require_capability("opa", "no FORGEOPS_TEST_OPA_URL and no docker on PATH to start one")

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
            f"{POLICY_DIR.as_posix()}:/policies:ro",
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
        port = subprocess.run(  # noqa: S603
            [docker, "port", name, "8181/tcp"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()[0]
        url = f"http://127.0.0.1:{port.rsplit(':', 1)[-1].strip()}"
        if not _wait_until_healthy(url):
            require_capability("opa", "the OPA container never became healthy")
        yield url
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, check=False)  # noqa: S603


@pytest.fixture()
async def policy(opa_url: str):
    async with httpx.AsyncClient(timeout=5.0) as http:
        yield OpaGatewayPolicy(opa_url=opa_url, http=http)


pytestmark = pytest.mark.asyncio


class TestFilterAgainstRealOpa:
    async def test_read_only_agent_sees_only_the_read_only_tool(self, policy):
        allowed = await policy.filter_tools(server="agent", tools=ALL_TOOLS, claims=CLAIMS, blast_radius="read_only")

        assert [t["name"] for t in allowed] == [READ_ONLY_TOOL["name"]]

    async def test_workspace_agent_sees_read_only_and_workspace(self, policy):
        allowed = await policy.filter_tools(server="agent", tools=ALL_TOOLS, claims=CLAIMS, blast_radius="workspace")

        assert [t["name"] for t in allowed] == [READ_ONLY_TOOL["name"], WORKSPACE_TOOL["name"]]

    async def test_infrastructure_agent_sees_everything_including_the_unannotated_tool(self, policy):
        allowed = await policy.filter_tools(
            server="agent", tools=ALL_TOOLS, claims=CLAIMS, blast_radius="infrastructure"
        )

        assert [t["name"] for t in allowed] == [t["name"] for t in ALL_TOOLS]

    async def test_an_unannotated_tool_is_hidden_from_a_read_only_agent(self, policy):
        """Unknown metadata must default to the HIGHEST blast radius (§11.4)."""
        allowed = await policy.filter_tools(
            server="agent", tools=[UNANNOTATED_TOOL], claims=CLAIMS, blast_radius="read_only"
        )

        assert allowed == []


class TestAuthoriseCallAgainstRealOpa:
    async def _authorise(self, policy, tool: dict, radius: str) -> bool:
        from src.core.errors import ProblemException

        try:
            await policy.authorise_call(
                server="agent",
                tool=tool["name"],
                metadata={"tool_descriptor": tool},
                claims=CLAIMS,
                blast_radius=radius,
            )
            return True
        except ProblemException as exc:
            assert exc.problem.status == 403
            return False

    async def test_read_only_tool_is_allowed_for_a_read_only_agent(self, policy):
        assert await self._authorise(policy, READ_ONLY_TOOL, "read_only") is True

    async def test_infrastructure_tool_is_denied_for_a_read_only_agent(self, policy):
        assert await self._authorise(policy, INFRA_TOOL, "read_only") is False

    async def test_infrastructure_tool_is_allowed_for_an_infrastructure_agent(self, policy):
        assert await self._authorise(policy, INFRA_TOOL, "infrastructure") is True

    async def test_unknown_tool_defaults_to_the_highest_radius_and_is_denied(self, policy):
        assert await self._authorise(policy, UNANNOTATED_TOOL, "read_only") is False
        assert await self._authorise(policy, UNANNOTATED_TOOL, "workspace") is False


class TestPolicyPathAgreement:
    """The exact regression that shipped: a queried path with no such rule."""

    async def test_the_default_paths_resolve_to_a_defined_document(self, policy):
        # If this returns [] for an infrastructure agent, the path is wrong.
        allowed = await policy.filter_tools(
            server="agent", tools=[READ_ONLY_TOOL], claims=CLAIMS, blast_radius="infrastructure"
        )
        assert allowed, f"{DEFAULT_FILTER_PATH} resolved to an undefined document"

    async def test_a_wrong_package_path_raises_instead_of_silently_denying(self, opa_url):
        """A missing policy must fail closed LOUDLY, not look like a deny."""
        from src.core.errors import ProblemException

        async with httpx.AsyncClient(timeout=5.0) as http:
            misconfigured = OpaGatewayPolicy(
                opa_url=opa_url,
                filter_path="/v1/data/forgeops/mcp/filter_tools",  # the shipped bug
                allow_path="/v1/data/forgeops/mcp/allow_call",  # the shipped bug
                http=http,
            )

            with pytest.raises(ProblemException) as filter_exc:
                await misconfigured.filter_tools(
                    server="agent", tools=ALL_TOOLS, claims=CLAIMS, blast_radius="infrastructure"
                )
            assert filter_exc.value.problem.status == 503
            assert "policy-undefined" in filter_exc.value.problem.type

            with pytest.raises(ProblemException) as allow_exc:
                await misconfigured.authorise_call(
                    server="agent",
                    tool=READ_ONLY_TOOL["name"],
                    metadata={"tool_descriptor": READ_ONLY_TOOL},
                    claims=CLAIMS,
                    blast_radius="infrastructure",
                )
            assert allow_exc.value.problem.status == 503

    async def test_an_unreachable_opa_still_fails_closed_quietly(self):
        """Transport failure keeps the old behaviour: empty list, then 403."""
        from src.core.errors import ProblemException

        async with httpx.AsyncClient(timeout=0.05) as http:
            # Reserved-for-documentation address; nothing listens.
            dead = OpaGatewayPolicy(opa_url="http://192.0.2.1:8181", http=http)

            assert (
                await dead.filter_tools(server="agent", tools=ALL_TOOLS, claims=CLAIMS, blast_radius="infrastructure")
                == []
            )
            with pytest.raises(ProblemException) as exc:
                await dead.authorise_call(
                    server="agent",
                    tool=READ_ONLY_TOOL["name"],
                    metadata={"tool_descriptor": READ_ONLY_TOOL},
                    claims=CLAIMS,
                    blast_radius="infrastructure",
                )
            assert exc.value.problem.status == 403

    async def test_the_allow_rule_name_matches_what_the_backend_queries(self, opa_url):
        """Ask OPA directly which rules `mcp.gateway` exposes."""
        async with httpx.AsyncClient(timeout=5.0) as http:
            resp = await http.post(
                f"{opa_url}{DEFAULT_ALLOW_PATH}",
                json={
                    "input": {
                        "tools": [READ_ONLY_TOOL],
                        "tool": READ_ONLY_TOOL["name"],
                        "agent_blast_radius": "read_only",
                    }
                },
            )
        assert resp.status_code == 200
        assert "result" in resp.json(), f"{DEFAULT_ALLOW_PATH} is not a defined document"
