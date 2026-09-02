#!/usr/bin/env python3
# SPDX-License-Identifier: FSL-1.1-ALv2
"""Drive a full apply, backup, revert and rollback from a HOST agent binary.

WHAT THIS EXISTS TO CATCH. Every other proof of `changeset.apply` runs the agent INSIDE the Compose
network. That is a real test of the protocol and no test at all of the deployment users actually have:
a signed command has to cross a published port, be verified against a CA the agent was issued rather
than one baked into an image, and write files onto the host's own filesystem with the host's own path
and permission semantics. Step 8 of onboarding was impossible for a host agent for exactly that reason
— `docker-compose.yml` had no mTLS listener at all, so nothing was listening on the port to dial.

WHAT IT ASSERTS, in order:
  1. `pair` succeeds over the ORDINARY port, which is where the one unauthenticated route lives.
  2. `doctor` reports the session endpoint came from the backend and that the listener VERIFIES
     against the CA stored at pairing. That is a real TLS handshake, not a configuration read.
  3. a `create` change set reaches `applied` and the file exists with the right bytes, with its
     POSIX-style relative path materialised natively.
  4. an `update` reaches `applied`, leaves a timestamped backup holding the exact pre-image, and the
     new bytes are in place.
  5. `revert` produces a reverse set that, once approved and applied, restores the file BYTE FOR BYTE
     and moves the original to `reverted`.
  6. a change set whose second write cannot succeed reaches `rolled_back` — a different state from
     `reverted` — and leaves the workspace as it was.

THE AGENT MUST BE RUNNING BEFORE A SUBMIT. `chokepoint.submit` calls `sink.send_command`, which raises
"No agent connected" when no session is live; a command is delivered to a live socket rather than
queued for a future one. So this starts the agent, waits for the session, and only then submits.

Run from the repository root with the stack already up:

    python scripts/ci/host-apply-proof.py --agent ./forgeops-agent --workspace /tmp/hostws
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

#: How long to wait for a change set to leave `applying`. Generous, because a cold agent has to
#: complete a TLS handshake and a policy check first, and a flaky timeout teaches people to re-run
#: rather than to read.
STATUS_TIMEOUT_SECONDS = 120

#: How long to wait for the session before submitting anything.
SESSION_TIMEOUT_SECONDS = 90

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml"]

UPDATE_OLD = '{"name":"host-fixture","version":"1.0.0","dependencies":{"express":"4.19.2"}}'
UPDATE_NEW = '{"name":"host-fixture","version":"1.1.0","dependencies":{"express":"4.19.2"}}'


class Failure(RuntimeError):
    """A proof step did not hold. The message is the report."""


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    workspace = Path(args.workspace).resolve()
    state = Path(args.state).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)

    # The fixture, written with EXPLICIT bytes and a trailing-newline-free body. `change_items.old_hash`
    # is the SHA-256 of what the agent is about to overwrite, so a stray byte — a BOM, a CRLF — makes
    # the §6.3 stale-apply guard abort the set. That is the guard working, and it cost a run to learn.
    (workspace / "package.json").write_bytes(UPDATE_OLD.encode("utf-8"))
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "src" / "index.js").write_bytes(
        b"const express = require('express'); express().listen(3000);"
    )
    # A directory the rollback case cannot write a file over, on any platform.
    (workspace / "rollback" / "blocked").mkdir(parents=True, exist_ok=True)

    seeded = seed(workspace)
    report(f"seeded project {seeded['project_id']} with code {seeded['code']}")

    env = agent_env(state, workspace)
    pair(args.agent, seeded["code"], args.backend, env)
    check_doctor(args.agent, env)

    with running_agent(args.agent, env, state) as agent:
        wait_for_session(seeded["project_id"])
        prove_create(seeded, workspace)
        prove_update(seeded, workspace)
        prove_revert(seeded, workspace)
        prove_rollback(seeded, workspace)
        del agent

    report("every host-apply assertion held")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, help="path to the built host binary")
    parser.add_argument("--workspace", required=True, help="a real host directory to apply into")
    parser.add_argument("--state", default=".host-apply-state", help="the agent's state directory")
    parser.add_argument(
        "--backend",
        default=None,
        help="the ORDINARY port's ws URL, where pairing happens. Defaults from BACKEND_PORT.",
    )
    args = parser.parse_args(argv)
    if args.backend is None:
        port = os.environ.get("BACKEND_PORT", "8000")
        args.backend = f"ws://localhost:{port}/api/v1/ws/agent"
    return args


def report(message: str) -> None:
    print(f"host-apply: {message}", flush=True)


def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kwargs)  # type: ignore[arg-type]


def seed(workspace: Path) -> dict[str, str]:
    """Create the project, publish the bundle and mint a code, through the real services."""
    copy = run(COMPOSE + ["cp", "scripts/seed_host_apply.py", "backend:/tmp/seed_host_apply.py"])
    if copy.returncode != 0:
        raise Failure(f"copying the seed script failed: {copy.stderr.strip()}")
    result = run(
        COMPOSE + ["exec", "-T", "backend", "python", "/tmp/seed_host_apply.py", str(workspace)]
    )
    if result.returncode != 0:
        raise Failure(f"seeding failed: {result.stdout[-2000:]}{result.stderr[-2000:]}")
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise Failure(f"the seed script printed no JSON: {result.stdout[-2000:]}")


def agent_env(state: Path, workspace: Path) -> dict[str, str]:
    """The agent's environment.

    `AGENT_BACKEND_WSS_URL` is deliberately ABSENT. Its presence with a `wss` value would win over the
    endpoint the backend states at pairing — which is correct for a containerised agent and would make
    this proof assert nothing about the pairing response. A host agent is told one URL, on the command
    line, and must learn the other.
    """
    env = dict(os.environ)
    env.pop("AGENT_BACKEND_WSS_URL", None)
    env["AGENT_STATE_DIR"] = str(state)
    env["AGENT_CREDENTIAL_STORE"] = "file"
    env["AGENT_WORKSPACE_ROOT"] = str(workspace)
    return env


def pair(agent: str, code: str, backend: str, env: dict[str, str]) -> None:
    result = run([agent, "pair", "--code", code, "--backend", backend], env=env)
    if result.returncode != 0:
        raise Failure(f"pair failed: {result.stdout}{result.stderr}")
    report("paired over the ordinary port")


def check_doctor(agent: str, env: dict[str, str]) -> None:
    """`doctor` must name the trust source and prove it with a handshake."""
    result = run([agent, "doctor"], env=env)
    out = result.stdout + result.stderr
    required = [
        "Session trust: verified against the CA bundle stored at pairing",
        "stated by the backend at pairing",
    ]
    for phrase in required:
        if phrase not in out:
            raise Failure(f"doctor did not report {phrase!r}. Output:\n{out}")
    report("doctor verified the listener against the CA issued at pairing")


class running_agent:  # noqa: N801 - a context manager reads better lowercase here
    """The agent as a host process, always stopped, with its log kept for a failure report."""

    def __init__(self, agent: str, env: dict[str, str], state: Path) -> None:
        self._argv = [agent, "run"]
        self._env = env
        self._log = state / "agent-run.log"

    def __enter__(self) -> subprocess.Popen[bytes]:
        self._handle = self._log.open("wb")
        self._proc = subprocess.Popen(self._argv, env=self._env, stdout=self._handle, stderr=self._handle)
        return self._proc

    def __exit__(self, *exc: object) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._handle.close()
        if exc[0] is not None:
            print(f"--- {self._log} ---", flush=True)
            print(self._log.read_text(encoding="utf-8", errors="replace")[-4000:], flush=True)


def sql(query: str) -> str:
    result = run(
        COMPOSE
        + ["exec", "-T", "postgres", "psql", "-U", "forgeops", "-d", "forgeops", "-tAc", query]
    )
    if result.returncode != 0:
        raise Failure(f"query failed: {query}\n{result.stderr.strip()}")
    return result.stdout.strip()


def wait_for_session(project_id: str) -> None:
    deadline = time.monotonic() + SESSION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        row = sql(
            "SELECT status FROM agent_devices "
            f"WHERE project_id = '{project_id}' AND status = 'active' LIMIT 1"
        )
        if row == "active":
            report("the host agent holds an active session over mutual TLS")
            return
        time.sleep(2)
    raise Failure("the host agent never reached `active`; see the agent log above")


def submit(seeded: dict[str, str], *mode: str) -> dict[str, str]:
    copy = run(COMPOSE + ["cp", "scripts/submit_host_apply.py", "backend:/tmp/submit_host_apply.py"])
    if copy.returncode != 0:
        raise Failure(f"copying the submit script failed: {copy.stderr.strip()}")
    result = run(
        COMPOSE
        + [
            "exec",
            "-T",
            "backend",
            "python",
            "/tmp/submit_host_apply.py",
            seeded["project_id"],
            seeded["user_id"],
            *mode,
        ]
    )
    if result.returncode != 0:
        raise Failure(f"submit {mode} failed: {result.stdout[-2000:]}{result.stderr[-2000:]}")
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise Failure(f"submit {mode} printed no JSON: {result.stdout[-2000:]}")


def await_status(change_set_id: str, expected: str) -> None:
    deadline = time.monotonic() + STATUS_TIMEOUT_SECONDS
    seen = ""
    while time.monotonic() < deadline:
        seen = sql(f"SELECT status FROM change_sets WHERE id = '{change_set_id}'")
        if seen == expected:
            return
        if seen not in ("applying", "approved", "pending_approval"):
            break
        time.sleep(2)
    raise Failure(
        f"change set {change_set_id} is {seen!r}, expected {expected!r}\n{diagnose(change_set_id)}"
    )


def diagnose(change_set_id: str) -> str:
    """What a stalled change set looks like from the database, so a timeout is not a dead end.

    A set stuck at `applying` has exactly three interesting causes and this separates them: the audit
    trail says whether it was ever authorised, the item list says what it was going to do, and the
    device row says whether the agent that should have received it still held a session. Without this
    the failure message names a UUID and a status and nothing else.
    """
    lines = [f"--- diagnosis for {change_set_id} ---"]
    for label, query in (
        (
            "audit",
            "SELECT action || ' | ' || outcome FROM audit_events "
            f"WHERE resource_id = '{change_set_id}' ORDER BY seq",
        ),
        (
            "items",
            "SELECT action || ' ' || file_path FROM change_items "
            f"WHERE change_set_id = '{change_set_id}' ORDER BY file_path",
        ),
        (
            # EVERY set on the project, because the most likely cause of "finalised but still
            # `applying`" is that the id being polled is not the id the hub moved. Seeing them all
            # side by side answers that in one line instead of another run.
            "all sets",
            "SELECT id || '=' || status FROM change_sets WHERE project_id = "
            f"(SELECT project_id FROM change_sets WHERE id = '{change_set_id}') "
            "ORDER BY created_at",
        ),
        (
            "devices",
            "SELECT id || ' ' || status || ' last_seen=' || COALESCE(last_seen::text, 'never') "
            "FROM agent_devices WHERE project_id = "
            f"(SELECT project_id FROM change_sets WHERE id = '{change_set_id}')",
        ),
    ):
        try:
            answer = sql(query) or "(none)"
        except Failure as exc:  # a diagnosis must not replace the real failure with its own
            answer = f"unavailable ({exc})"
        # Flattened onto one line per label: a multi-row answer split across lines is easy to lose in
        # a CI log, and losing half the evidence is how the first attempt at this cost a run.
        lines.append(f"{label}: {answer.replace(chr(10), ' / ')}")

    # The command → change-set mapping the hub uses to decide WHICH set a report is about. If this is
    # missing the report cannot be attributed, and the hub says so rather than guessing.
    try:
        keys = run(
            COMPOSE + ["exec", "-T", "redis", "redis-cli", "--scan", "--pattern", "forgeops:cmdchangeset:*"]
        )
        lines.append(f"command mappings: {(keys.stdout or '').strip().replace(chr(10), ' / ') or '(none)'}")
    except Exception as exc:  # noqa: BLE001 - diagnostics are best-effort by definition
        lines.append(f"command mappings: unavailable ({type(exc).__name__})")
    return "\n".join(lines)


def prove_create(seeded: dict[str, str], workspace: Path) -> None:
    result = submit(seeded)
    await_status(result["change_set_id"], "applied")
    target = workspace / "deploy" / "Dockerfile"
    if not target.is_file():
        raise Failure(f"{target} was not written; the apply reported applied but produced no file")
    body = target.read_text(encoding="utf-8")
    if "FROM node:20-alpine" not in body:
        raise Failure(f"{target} does not hold the change set's content:\n{body}")
    # The wire carries `deploy/Dockerfile`; a platform that failed to translate it would produce a
    # single entry literally named `deploy/Dockerfile` rather than a directory containing a file.
    if not (workspace / "deploy").is_dir():
        raise Failure("the relative path was not materialised as a native directory")
    report("a create change set applied and its file exists with the right bytes")


def prove_update(seeded: dict[str, str], workspace: Path) -> None:
    target = workspace / "package.json"
    before = target.read_bytes()
    result = submit(seeded, "update")
    await_status(result["change_set_id"], "applied")
    after = target.read_bytes()
    if after.decode("utf-8") != UPDATE_NEW:
        raise Failure(f"package.json holds {after!r}, expected the updated content")
    backups = sorted(workspace.glob("package.json.backup.*"))
    if not backups:
        raise Failure("no backup was taken before an overwrite")
    kept = backups[-1].read_bytes()
    if kept != before:
        raise Failure(
            f"the backup is not the pre-image: {sha(kept)} != {sha(before)} "
            f"({len(kept)} vs {len(before)} bytes)"
        )
    report(f"an update applied and {backups[-1].name} holds the exact pre-image")


def prove_revert(seeded: dict[str, str], workspace: Path) -> None:
    target = workspace / "package.json"
    backups = sorted(workspace.glob("package.json.backup.*"))
    pre_image = backups[-1].read_bytes()

    applied = newest_change_set(seeded["project_id"], "applied", "package.json")
    # BOTH HALVES IN ONE CALL. A revert is itself a mutation needing authority, so the reverse set
    # arrives `pending_approval`; splitting the revert and its approval across two `exec` invocations
    # meant rediscovering the reverse id by "newest pending_approval", which is a guess about which
    # row is meant. The script now returns the id it created.
    result = submit(seeded, "revert-and-approve", applied)
    reverse = result["reverse_change_set_id"]
    await_status(reverse, "applied")
    await_status(applied, "reverted")

    restored = target.read_bytes()
    if restored != pre_image:
        raise Failure(
            f"revert did not restore byte for byte: {sha(restored)} != {sha(pre_image)}"
        )
    report("revert restored the file byte for byte and the original moved to `reverted`")


def prove_rollback(seeded: dict[str, str], workspace: Path) -> None:
    first = workspace / "rollback" / "first.txt"
    if first.exists():
        first.unlink()
    result = submit(seeded, "rollback")
    await_status(result["change_set_id"], "rolled_back")
    if first.exists():
        raise Failure(f"{first} survived a rollback; the workspace was not restored")
    action = sql(
        "SELECT action FROM audit_events "
        f"WHERE resource_id = '{result['change_set_id']}' AND action = 'change_set_rolled_back'"
    )
    if action != "change_set_rolled_back":
        raise Failure("a rollback left no audit record, only a log line")
    report("a partly-applied change set rolled back, reached `rolled_back` and left a record")


def newest_change_set(project_id: str, status: str, mentions: str | None) -> str:
    clause = ""
    if mentions is not None:
        clause = (
            " AND EXISTS (SELECT 1 FROM change_items ci WHERE ci.change_set_id = cs.id "
            f"AND ci.file_path = '{mentions}')"
        )
    found = sql(
        "SELECT cs.id FROM change_sets cs "
        f"WHERE cs.project_id = '{project_id}' AND cs.status = '{status}'{clause} "
        "ORDER BY cs.created_at DESC LIMIT 1"
    )
    if not found:
        raise Failure(f"no change set on {project_id} is {status!r}")
    return found


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Failure as failure:
        print(f"host-apply FAILED: {failure}", file=sys.stderr, flush=True)
        raise SystemExit(1) from failure
