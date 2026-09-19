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
            else:
                raise SystemExit(f"unknown action {action!r}")
    return 0


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
