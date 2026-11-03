# SPDX-License-Identifier: FSL-1.1-ALv2
"""Drive one readiness cycle through the REAL routes: score, generate, approve.

WHY THIS EXISTS. The baseline measurement the product's own value rests on is "what did the score read
before a generation run, and what did it read after the apply landed". Nothing measured that end to
end: `test_reachable_score_contract.py` asserts 90 -> 100 against the template renderer inside a unit
test, and the journey spec proves the loop runs without recording the two numbers together.

WHAT IS REAL HERE AND WHAT IS NOT. Every route body runs: `GET /projects/{id}/readiness` reads the rows
an agent scan persisted, `POST /generation/runs` calls the model and submits through the governance
chokepoint, `POST /approvals/{id}/approve` mints authority and delivers the signed envelope over the
Redis-backed hub to whichever replica owns the agent's socket. What is substituted is exactly one thing
- `require_principal`, because the alternative is a browser and an OIDC round trip, which is what
`seed_host_apply.py` already established for the same reason. Nothing else is overridden: no fake sink,
no fake model, no fake scorer.

Run inside the backend container:

    docker compose exec -T backend python /tmp/cycle_drive.py score    <project-id> <user-id>
    docker compose exec -T backend python /tmp/cycle_drive.py generate <project-id> <user-id> [prompt]
    docker compose exec -T backend python /tmp/cycle_drive.py approve  <project-id> <user-id> <change-set-id>

Each subcommand prints one JSON object on the last line.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

import httpx
from sqlalchemy import text

from src.auth.dependencies import require_principal
from src.auth.principal import Principal, UserRole
from src.main import create_app

#: A real CPU completion takes over 100 seconds and the service may take three attempts, so the read
#: bound has to be generous. A 60-second client timeout once made every call abort and fall back to
#: templates silently, which is the failure this number exists to avoid.
GENERATION_TIMEOUT_SECONDS = 3600.0


async def principal_for(session: Any, user_id: uuid.UUID) -> Principal:
    """The real `Principal` for a stored user, read from the row rather than invented."""
    row = (
        (
            await session.execute(
                text("SELECT email, idp_subject, role FROM users WHERE id = :id"),
                {"id": user_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise SystemExit(f"no user {user_id}")
    return Principal.for_user(
        user_id=user_id,
        subject=str(row["idp_subject"]),
        email=str(row["email"]),
        role=UserRole(str(row["role"])),
    )


async def main(argv: list[str]) -> int:
    if len(argv) < 3:
        raise SystemExit(__doc__)
    action, project_id, user_id = argv[0], uuid.UUID(argv[1]), uuid.UUID(argv[2])
    rest = argv[3:]

    app = create_app()
    async with app.router.lifespan_context(app):
        async with app.state.sessionmaker() as session:
            principal = await principal_for(session, user_id)

        app.dependency_overrides[require_principal] = lambda: principal
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://cycle.invalid",
            timeout=GENERATION_TIMEOUT_SECONDS,
        ) as client:
            if action == "score":
                await score(client, project_id)
            elif action == "generate":
                await generate(
                    client,
                    project_id,
                    rest[0] if rest else "make this project deployable",
                )
            elif action == "approve":
                await approve(client, uuid.UUID(rest[0]))
            elif action == "link-github":
                # THE TOKEN ARRIVES ON STDIN, not as an argument: an argument is visible in `ps` to
                # every other user on the machine, which is one of the exposures Part 3 forbids.
                await link_github(client, sys.stdin.read().strip())
            elif action == "list-repos":
                await list_repos(client, rest[0] if rest else "")
            elif action == "create-from-github":
                await create_from_github(
                    client, rest[0], rest[1] if len(rest) > 1 else ""
                )
            elif action == "clone":
                await clone(client, project_id)
            elif action == "pair-code":
                await pair_code(app, principal, uuid.UUID(rest[0]))
            else:
                raise SystemExit(f"unknown action {action!r}")
    return 0


async def pair_code(app: Any, principal: Any, target_project: uuid.UUID) -> None:
    """Mint a pairing code for a project through the composed `DeviceService`.

    NOT through `POST /agents/pairing-codes`: that route carries `require_role` as a ROUTER dependency,
    and this driver overrides only `require_principal` — overriding the role check as well would be
    substituting the authorisation this repository's tests exist to prove. The service is the same
    object the route would have called, which is the substitution `seed_host_apply.py` already argues
    for: a real `DeviceService`, the real internal CA, the real Redis, the real pepper.
    """
    async with app.state.sessionmaker() as session:
        issued = await app.state.device_service.issue_pairing_code(
            session, project_id=target_project, actor=principal
        )
        await session.commit()
    print(json.dumps({"code": issued.code, "device_id": str(issued.device_id)}))


async def link_github(client: httpx.AsyncClient, token: str) -> None:
    """Link with a pasted token — the path that needs no GitHub App and shows no GitHub screen."""
    response = await client.put(
        "/api/v1/integrations/github/token", json={"token": token}
    )
    if response.status_code >= 400:
        raise SystemExit(f"link refused {response.status_code}: {response.text[:600]}")
    body = response.json()
    print(
        json.dumps({"login": body["login"], "credential_kind": body["credential_kind"]})
    )


async def list_repos(client: httpx.AsyncClient, query: str) -> None:
    """The real listing, as the picker reads it."""
    response = await client.get(
        "/api/v1/integrations/github/repositories",
        params={"query": query, "per_page": 5},
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"listing refused {response.status_code}: {response.text[:600]}"
        )
    body = response.json()
    print(
        json.dumps(
            {
                "total": body["total"],
                "truncated": body["truncated"],
                "items": [
                    {
                        "full_name": item["full_name"],
                        "private": item["private"],
                        "default_branch": item["default_branch"],
                        "language": item["language"],
                    }
                    for item in body["items"]
                ],
            }
        )
    )


async def create_from_github(
    client: httpx.AsyncClient, full_name: str, parent: str
) -> None:
    """Create the project, then read the index status so the `awaiting_clone` state is visible."""
    response = await client.post(
        "/api/v1/projects/from-github",
        json={
            "repo_full_name": full_name,
            "parent_directory": parent,
            "directory_name": "",
            "branch": "",
        },
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"create refused {response.status_code}: {response.text[:600]}"
        )
    body = response.json()
    status = await client.get(f"/api/v1/analysis/codebase/{body['id']}/status")
    print(
        json.dumps(
            {
                "project_id": body["id"],
                "name": body["name"],
                "path": body["path"],
                "clone_state": body["settings"].get("clone_state"),
                "index_status": status.json().get("status"),
            }
        )
    )


async def clone(client: httpx.AsyncClient, project_id: uuid.UUID) -> None:
    """Dispatch the clone through the governance chokepoint."""
    response = await client.post(f"/api/v1/projects/{project_id}/clone")
    if response.status_code >= 400:
        raise SystemExit(f"clone refused {response.status_code}: {response.text[:900]}")
    print(json.dumps(response.json()))


async def score(client: httpx.AsyncClient, project_id: uuid.UUID) -> None:
    response = await client.get(f"/api/v1/projects/{project_id}/readiness")
    response.raise_for_status()
    body = response.json()
    short = [
        f"{check['id']} {check['points']}/{check['max_points']}"
        for check in body.get("checks", [])
        if check["points"] < check["max_points"]
    ]
    print(
        json.dumps(
            {
                "score": body["score"],
                "level": body["level"],
                "indexed": body["indexed"],
                "evaluated_paths": body["evaluated_paths"],
                "reachable_score": body.get("reachable_score"),
                "categories": body.get("categories"),
                "short_checks": short,
            }
        )
    )


async def generate(
    client: httpx.AsyncClient, project_id: uuid.UUID, prompt: str
) -> None:
    """Consume the SSE stream and report its terminal frame plus the change set it produced."""
    frames: list[dict[str, Any]] = []
    tokens = 0
    async with client.stream(
        "POST",
        "/api/v1/generation/runs",
        json={
            "project_id": str(project_id),
            "prompt": prompt,
            "environment": "staging",
        },
    ) as response:
        if response.status_code != 200:
            await response.aread()
            raise SystemExit(
                f"generation refused {response.status_code}: {response.text[:2000]}"
            )
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            frame = json.loads(line[5:].strip())
            if frame.get("event") == "token":
                tokens += 1
                continue
            frames.append(frame)

    terminal = frames[-1] if frames else {}
    print(
        json.dumps(
            {
                "token_frames": tokens,
                "terminal_event": terminal.get("event"),
                "terminal": terminal,
                "frames": frames[-4:],
            }
        )
    )


async def approve(client: httpx.AsyncClient, change_set_id: uuid.UUID) -> None:
    detail = await client.get(f"/api/v1/approvals/{change_set_id}")
    detail.raise_for_status()
    version = detail.json()["version"]
    response = await client.post(
        f"/api/v1/approvals/{change_set_id}/approve",
        json={"comment": "baseline cycle measurement", "expected_version": version},
    )
    if response.status_code >= 400:
        raise SystemExit(
            f"approve refused {response.status_code}: {response.text[:2000]}"
        )
    print(json.dumps({"approve": response.json()}))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
