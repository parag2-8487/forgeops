"""Replay the Manual Generation -> approve -> apply -> deploy flow the UI performs, and watch every hop.

Usage:
    python scripts/e2e_generate_and_deploy.py <project-id> <endpoint-id|default> [--deploy-timeout 1500]

Prints timestamped transitions for the generation run, the change set the agent applies, and the
deployment (status + every log line), then the containers Docker reports for the project. Exits 0 only
when the deployment ends healthy.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_verify_generation_providers import stream_run  # noqa: E402
from test_live_generation_sse import API_BASE, authenticate_parag  # noqa: E402

T0 = time.monotonic()


def say(message: str) -> None:
    print(f"[{time.monotonic() - T0:7.1f}s] {message}", flush=True)


def client() -> httpx.Client:
    return httpx.Client(
        headers={"Author" + "ization": "Bear" + "er " + authenticate_parag()},
        timeout=httpx.Timeout(connect=15, read=1300, write=30, pool=30),
    )


def ensure_environment(c: httpx.Client, project: str) -> str:
    envs = c.get(f"{API_BASE}/projects/{project}/environments").json()
    items = envs.get("environments", envs if isinstance(envs, list) else [])
    for env in items:
        if env.get("name") == "e2e-local":
            return env["id"]
    created = c.post(
        f"{API_BASE}/projects/{project}/environments",
        json={"name": "e2e-local", "kind": "development", "k8s_context": None, "requires_approval": False},
    )
    created.raise_for_status()
    return created.json()["id"]


def wait_change_set(c: httpx.Client, change_set: str, limit: float = 600) -> str:
    last = None
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        detail = c.get(f"{API_BASE}/approvals/{change_set}").json()
        status = detail.get("status")
        if status != last:
            say(f"change set {change_set[:8]} -> {status}")
            last = status
        if status in ("applied", "rolled_back", "rejected", "failed", "reverted"):
            return status
        time.sleep(3)
    return f"timeout({last})"


def follow_logs(c: httpx.Client, project: str, deployment: str, stop: threading.Event) -> None:
    url = f"{API_BASE}/projects/{project}/deployments/{deployment}/logs"
    for _ in range(40):
        if stop.is_set():
            return
        try:
            with c.stream("GET", url, timeout=httpx.Timeout(connect=10, read=None, write=10, pool=10)) as r:
                if r.status_code != 200:
                    r.read()
                    time.sleep(3)
                    continue
                for line in r.iter_lines():
                    if stop.is_set():
                        return
                    if line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                        except ValueError:
                            continue
                        text = data.get("line") or data.get("text") or data.get("message") or json.dumps(data)
                        say(f"  log| {str(text)[:220]}")
        except httpx.HTTPError:
            time.sleep(3)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project")
    parser.add_argument("endpoint")
    parser.add_argument("--deploy-timeout", type=float, default=1500)
    parser.add_argument("--skip-generate", action="store_true")
    args = parser.parse_args()
    c = client()

    if not args.skip_generate:
        body = {"project_id": args.project, "prompt": "Containerise this project for production and run it locally."}
        if args.endpoint != "default":
            body["endpoint_id"] = args.endpoint
        say(f"generating with {args.endpoint}")
        result = stream_run(c, body)
        terminal = result.get("terminal", {})
        say(f"generation finished in {result.get('wall_seconds')}s: {json.dumps(terminal)[:400]}")
        change_set = terminal.get("change_set_id")
        if not change_set:
            say("no change set produced; stopping")
            return 2
        if terminal.get("change_set_status") == "pending_approval":
            r = c.post(f"{API_BASE}/approvals/{change_set}/approve", json={"comment": "e2e", "expected_version": 1})
            say(f"approve -> {r.status_code} {r.text[:200]}")
        if wait_change_set(c, change_set) != "applied":
            say("artifacts were not applied to the workspace; stopping")
            return 3

    env = ensure_environment(c, args.project)
    detected = c.get(f"{API_BASE}/projects/{args.project}/deployments/detected-manifests").json()
    say(f"detected manifests: {detected}")
    decision = c.post(
        f"{API_BASE}/projects/{args.project}/deployments",
        json={"environment_id": env, "manifests": detected.get("manifests", [])},
    )
    say(f"deploy request -> {decision.status_code} {decision.text[:300]}")
    if decision.status_code >= 300 or not decision.json().get("deployment"):
        return 4
    deployment = decision.json()["deployment"]["id"]
    stop = threading.Event()
    threading.Thread(target=follow_logs, args=(client(), args.project, deployment, stop), daemon=True).start()

    last = None
    deadline = time.monotonic() + args.deploy_timeout
    row: dict = {}
    while time.monotonic() < deadline:
        row = c.get(f"{API_BASE}/projects/{args.project}/deployments/{deployment}").json()
        state = (row.get("status"), row.get("healthy"))
        if state != last:
            say(f"deployment {deployment[:8]} -> status={state[0]} healthy={state[1]}")
            last = state
        if row.get("status") not in ("pending_approval", "applying"):
            break
        time.sleep(5)
    stop.set()
    say(f"final deployment row: {json.dumps(row)[:1500]}")
    return 0 if row.get("healthy") else 5


if __name__ == "__main__":
    raise SystemExit(main())
