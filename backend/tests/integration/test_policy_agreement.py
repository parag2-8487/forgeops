import json
import subprocess
import uuid
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


# Generate policy parameters
@st.composite
def policy_parameters(draw) -> dict:
    return {
        "timezone": draw(st.sampled_from(["UTC", "Asia/Kolkata", "America/Los_Angeles"])),
        "blocked_weekdays": draw(st.lists(st.sampled_from(["Monday", "Friday", "Sunday"]), max_size=3)),
        "blocked_window": {
            "start_hour": draw(st.integers(min_value=0, max_value=12)),
            "end_hour": draw(st.integers(min_value=13, max_value=23)),
        },
        "protected_globs": ["**/package.json"],
    }


@st.composite
def governance_inputs(draw) -> dict:
    return {
        "operation": draw(st.sampled_from(["changeset.apply", "changeset.plan"])),
        "project_id": str(uuid.uuid4()),
        "tenant_id": None,
        "device_id": str(uuid.uuid4()),
        "bundle_digest": "sha256:" + "ab" * 32,
        "change_set_id": None,
        "environment": draw(st.sampled_from(["dev", "staging", "prod"])),
        "policy_parameters": draw(policy_parameters()),
        "principal": {
            "kind": "user",
            "role": draw(st.sampled_from(["maintainer", "developer", "admin"])),
            "blast_radius": "workspace",
            "user_id": "u-1",
        },
        "items": [
            {
                "file_path": draw(st.sampled_from(["src/index.ts", "package.json", "docs/README.md"])),
                "action": draw(st.sampled_from(["modify", "create", "delete"])),
            }
        ],
        "now": draw(st.sampled_from(["2026-08-07T02:30:00Z", "2026-08-05T10:00:00Z", "2026-08-09T12:00:00Z"])),
    }


@pytest.fixture(scope="module")
def shared_bundle_path(tmp_path_factory) -> Path:
    # Instead of hitting DB in a sync property test, we just tar the rego files manually to create a bundle.
    import gzip
    import io
    import tarfile

    agent_dir = Path(__file__).parent.parent.parent.parent / "policies" / "agent"

    tar_buf = io.BytesIO()
    entries = []

    # Dummy data.json
    data_bytes = json.dumps({"forgeops": {"governance": {"policies": []}}}).encode("utf-8")
    entries.append(("data.json", data_bytes))

    for path in agent_dir.glob("*.rego"):
        if path.is_file():
            entries.append((path.name, path.read_bytes()))

    entries.sort(key=lambda x: x[0])
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(content))

    gz_buf = io.BytesIO()
    with gzip.GzipFile(fileobj=gz_buf, mode="wb", mtime=0) as gz:
        gz.write(tar_buf.getvalue())

    bundle_path = tmp_path_factory.mktemp("bundle") / "bundle.tar.gz"
    bundle_path.write_bytes(gz_buf.getvalue())
    return bundle_path


@pytest.fixture(scope="module")
def evalhelper_path(tmp_path_factory) -> Path:
    agent_dir = Path(__file__).parent.parent.parent.parent / "agent"
    for name in ("evalhelper.exe", "evalhelper"):
        p = agent_dir / name
        if p.exists():
            return p
    # Try building it
    try:
        bin_path = tmp_path_factory.mktemp("bin") / "evalhelper"
        subprocess.run(["go", "build", "-o", str(bin_path), "./cmd/evalhelper"], cwd=str(agent_dir), check=True)
        return bin_path
    except Exception as exc:
        pytest.skip(f"evalhelper binary not built and go build failed: {exc}")


@settings(max_examples=25, deadline=None)
@given(input_payload=governance_inputs())
def test_agent_backend_agreement(
    input_payload: dict,
    shared_bundle_path: Path,
    evalhelper_path: Path,
    governance_opa_url: str,
) -> None:
    # 1. Evaluate via agent evalhelper
    input_json = json.dumps(input_payload)
    # Run evalhelper.exe
    proc = subprocess.run(
        [str(evalhelper_path), "-bundle", str(shared_bundle_path), "-input", input_json],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"evalhelper failed: {proc.stderr}"

    agent_result = json.loads(proc.stdout)
    assert "error" not in agent_result, agent_result["error"]
    agent_decision = agent_result["decision"]

    # 2. Evaluate via backend (OpaGovernancePolicy)
    # Since this is a sync test (hypothesis runs sync), we can use httpx.Client
    import httpx

    client = httpx.Client()
    resp = client.post(f"{governance_opa_url}/v1/data/forgeops/governance/decision", json={"input": input_payload})
    assert resp.status_code == 200

    opa_result = resp.json()
    if "result" not in opa_result:
        # Undefined document -> deny
        opa_decision = "deny"
    else:
        opa_decision = opa_result["result"]

    assert agent_decision == opa_decision, (
        f"Disagreement! Agent said {agent_decision}, OPA said {opa_decision} for input {input_payload}"
    )


@pytest.mark.asyncio
async def test_agent_fail_closed_drift(
    shared_bundle_path: Path,
    evalhelper_path: Path,
) -> None:
    # 1. Run evalhelper with a wrong expected digest
    input_payload = {
        "operation": "changeset.apply",
        "project_id": "8354a99d-011d-45d5-9d5e-7b45bf8f5ecb",
        "environment": "dev",
        "principal": {"kind": "user", "role": "developer", "blast_radius": "workspace", "user_id": "u-1"},
        "items": [],
        "now": "2026-08-01T00:00:00Z",
    }
    input_json = json.dumps(input_payload)

    proc = subprocess.run(
        [
            str(evalhelper_path),
            "-bundle",
            str(shared_bundle_path),
            "-input",
            input_json,
            "-expected-digest",
            "sha256:wrongdigest",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"evalhelper failed: {proc.stderr}"

    agent_result = json.loads(proc.stdout)

    assert "error" in agent_result, "Expected an error for drift"

    # 2. Check it fails closed
    agent_decision = agent_result["decision"]
    assert agent_decision["result"] == "deny", f"Agent should fail closed on drift, got {agent_decision}"
    assert "differs from envelope" in agent_decision["reason"].lower() or "drift" in agent_decision["reason"].lower(), (
        f"Agent should report drift in reason, got {agent_decision}"
    )
