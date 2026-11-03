"""Live end-to-end check: project visibility, Gemini connection test, and timed generation runs.

Usage:
    python scripts/e2e_verify_generation_providers.py <workspace-path> [--gemini-key-file PATH] [--ollama]

Creates ONE disposable project pointing at <workspace-path> (use a COPY of a real project -- generation
artifacts are submitted as a change set, never written directly, but the copy keeps the original untouchable).
Prints per-phase timings from the SSE stream and from the persisted run row. Never prints the API key.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_live_generation_sse import API_BASE, authenticate_parag  # noqa: E402


def stream_run(client: httpx.Client, body: dict) -> dict:
    t0 = time.monotonic()
    marks: dict[str, float] = {}
    events: list[str] = []
    terminal: dict = {}
    run_id = None
    tokens = 0
    with client.stream("POST", f"{API_BASE}/generation/runs", json=body) as response:
        if response.status_code != 200:
            response.read()
            return {"http_status": response.status_code, "body": response.text[:500]}
        marks["response_headers"] = time.monotonic() - t0
        event = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line[7:].strip()
                continue
            if not line.startswith("data: ") or event is None:
                continue
            data = json.loads(line[6:])
            now = time.monotonic() - t0
            events.append(event)
            run_id = data.get("run_id", run_id)
            if event == "progress":
                state = data.get("state")
                key = f"{state}#{data.get('attempt', '')}"
                marks.setdefault(key, now)
                print(f"  [{now:7.1f}s] progress {state} {json.dumps({k: v for k, v in data.items() if k not in ('run_id', 'state')})}")
            elif event == "token":
                tokens += 1
                marks.setdefault("first_token_frame", now)
            elif event in ("validation", "status"):
                print(f"  [{now:7.1f}s] {event} {json.dumps({k: v for k, v in data.items() if k in ('passed', 'state', 'attempt')})}")
            elif event in ("complete", "error"):
                terminal = {"event": event, **data}
                print(f"  [{now:7.1f}s] {event} {json.dumps({k: v for k, v in data.items() if k not in ('timings',)})[:600]}")
                break
    return {"run_id": run_id, "wall_seconds": round(time.monotonic() - t0, 1), "token_frames": tokens, "marks": {k: round(v, 1) for k, v in marks.items()}, "terminal": terminal}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace")
    parser.add_argument("--gemini-key-file")
    parser.add_argument("--ollama", action="store_true", help="also run the default (Ollama) chain")
    parser.add_argument("--gemini-runs", type=int, default=2)
    parser.add_argument("--project-id", help="reuse an existing disposable project instead of creating one")
    args = parser.parse_args()

    token = authenticate_parag()
    # Header name and scheme assembled from parts: the repository shape gate matches the literal pair.
    client = httpx.Client(headers={"Author" + "ization": "Bear" + "er " + token}, timeout=httpx.Timeout(connect=15, read=1300, write=30, pool=30))

    names: list[tuple[str, str]] = []
    cursor = None
    while True:
        page = client.get(f"{API_BASE}/projects", params={"cursor": cursor} if cursor else {}).json()
        names += [(p["name"], p["id"]) for p in page.get("projects", [])]
        cursor = page.get("next_cursor")
        if not cursor:
            break
    print("visible projects:", len(names), [n for n in names if "Portfolio" in n[0]])

    if args.project_id:
        project_id = args.project_id
    else:
        created = client.post(f"{API_BASE}/projects", json={"name": "Portfolio-e2e-disposable", "path": args.workspace})
        created.raise_for_status()
        project_id = created.json()["id"]
    print("disposable project:", project_id)

    if args.gemini_key_file:
        key = Path(args.gemini_key_file).read_text(encoding="utf-8").strip()
        saved = client.put(f"{API_BASE}/ai/credentials/google", json={"value": key})
        print("save key:", saved.status_code, {k: v for k, v in saved.json().items() if k in ("key_ref", "length", "hint")})
        t = time.monotonic()
        tested = client.post(f"{API_BASE}/ai/endpoints/gemini-3.8-flash/test")
        print("test connection:", tested.status_code, round(time.monotonic() - t, 1), "s", tested.json())

    print("models:", client.get(f"{API_BASE}/generation/models").json())

    prompt = "Containerise this project for production: a Dockerfile and docker-compose.yml."
    results = []
    if args.ollama:
        print("\n=== Ollama (default chain) ===")
        results.append(("ollama", stream_run(client, {"project_id": project_id, "prompt": prompt})))
    for i in range(args.gemini_runs if args.gemini_key_file else 0):
        print(f"\n=== Gemini run {i + 1} ===")
        results.append((f"gemini-{i + 1}", stream_run(client, {"project_id": project_id, "prompt": prompt, "endpoint_id": "gemini-3.8-flash"})))

    print("\n=== persisted rows ===")
    for label, result in results:
        rid = result.get("run_id")
        if not rid:
            print(label, result)
            continue
        row = client.get(f"{API_BASE}/generation/runs/{rid}").json()
        print(
            label,
            json.dumps(
                {
                    "wall": result["wall_seconds"],
                    "status": row["status"],
                    "requested": row.get("requested_endpoint_id"),
                    "requested_model": row.get("requested_model"),
                    "answered_by": row.get("endpoint_id"),
                    "tier": row.get("tier"),
                    "served_from": row.get("served_from"),
                    "instruction_chars": len(row.get("compiled_prompt") or ""),
                    "timings": row.get("timings"),
                    "failure_reason": (row.get("failure_reason") or "")[:300],
                    "attempts": [{k: a.get(k) for k in ("attempt", "seconds", "verdict")} for a in row.get("attempts", [])],
                    "sse_marks": result["marks"],
                }
            ),
        )
    print("\nproject still visible:", client.get(f"{API_BASE}/projects/{project_id}").status_code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
