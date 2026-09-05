# SPDX-License-Identifier: FSL-1.1-ALv2
"""Prove an EDIT to an existing file lands on disk and reverts byte-exact.

Not a test: a live proof against the running stack, because the thing being demonstrated is that the
pre-image the backend holds is the bytes the agent finds on the operator's disk. Only a real scan
followed by a real apply can establish that.

    python scripts/prove_patch_apply.py <project_id> <relative path> <mode>

    mode=submit   build an update change set from the index pre-image and submit it
    mode=revert   revert the named change set
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import uuid

from sqlalchemy import text

from src.auth.models import UserRole
from src.auth.principal import Principal
from src.core.errors import ProblemException
from src.governance.chokepoint import ChangeItemRequest, MutationRequest
from src.main import create_app

NEW_BODY = """# Patched by ForgeOps. The comment block below must survive.
FROM python:3.12-slim@sha256:0000000000000000000000000000000000000000000000000000000000000000 AS build
WORKDIR /src
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim@sha256:0000000000000000000000000000000000000000000000000000000000000000
WORKDIR /app
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY . .
RUN useradd --system --uid 10001 --no-create-home app
USER 10001
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import sys; sys.exit(0)"
CMD ["python", "src/main.py"]
"""


async def main() -> None:
    project_id = uuid.UUID(sys.argv[1])
    rel_path = sys.argv[2]
    mode = sys.argv[3]

    app = create_app()
    async with app.router.lifespan_context(app):
        chokepoint = app.state.governance_chokepoint
        async with app.state.sessionmaker() as session:
            row = (
                await session.execute(text("SELECT id, idp_subject, email FROM users LIMIT 1"))
            ).mappings().one()
            principal = Principal.for_user(
                user_id=row["id"],
                subject=row["idp_subject"],
                email=row["email"],
                role=UserRole.ADMIN,
            )

            if mode == "revert":
                cs = uuid.UUID(sys.argv[4])
                try:
                    result = await chokepoint.revert(session, change_set_id=cs, principal=principal)
                    await session.commit()
                    print(f"REVERT -> {result.status}")
                except ProblemException as exc:
                    print(f"REVERT REFUSED -> {exc.problem.type}: {exc.problem.detail}")
                return

            stored = (
                await session.execute(
                    text(
                        "SELECT c.content, c.redaction_count FROM file_contents c "
                        "JOIN file_tree f ON f.id = c.file_id "
                        "WHERE f.project_id = :p AND lower(f.path) = lower(:path)"
                    ),
                    {"p": project_id, "path": rel_path},
                )
            ).first()
            if stored is None:
                print(f"NOT INDEXED: {rel_path}")
                return
            pre_image, redactions = str(stored[0]), int(stored[1])
            print(f"pre-image: {len(pre_image)} chars, {redactions} redaction(s)")
            print(f"pre-image sha256: {hashlib.sha256(pre_image.encode()).hexdigest()[:16]}")
            if redactions:
                print("REFUSING: this file was redacted, so the stored text is not the bytes on disk")
                return

            submission = await chokepoint.submit(
                session,
                MutationRequest(
                    project_id=project_id,
                    items=(
                        ChangeItemRequest(
                            file_path=rel_path,
                            action="update",
                            old_content=pre_image,
                            new_content=NEW_BODY,
                        ),
                    ),
                    reason="prove a patch to an existing file",
                    origin="generation",
                    environment=None,
                ),
                principal=principal,
            )
            await session.commit()
            print(f"CHANGESET={submission.change_set_id}")
            print(f"submitted -> {submission.status}")

        async with app.state.sessionmaker() as session:
            item = (
                await session.execute(
                    text(
                        "SELECT action, old_hash, new_hash FROM change_items "
                        "WHERE change_set_id = :cs"
                    ),
                    {"cs": submission.change_set_id},
                )
            ).mappings().one()
            print(f"item action={item['action']} old_hash={item['old_hash'][:16]}")


asyncio.run(main())
