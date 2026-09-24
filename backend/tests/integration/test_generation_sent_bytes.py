# SPDX-License-Identifier: FSL-1.1-ALv2
"""The ROUTE and the SERVICE must send the model the same bytes for the same inputs. Phase 2 2.3.

WHY THIS FILE EXISTS
--------------------
The service path, driven directly on the criterion-10 fixture with the journey's own prompt, produced
`served_from='provider'`, `status='accepted'`, `iterations=2` and all six artifacts -- measured twice. The
ROUTE, on inputs that looked identical, recorded `template_fallback`. Reading eliminated every named
candidate: the recorded `compiled_prompt` was correct, the index was complete, the composed service takes
the same `max_attempts`, and `retrieval` is only consulted on the branch where no plan was compiled.

So the question is not what was COMPILED. It is what was SENT. The recorded column is written before the
stream and can agree with the compiled plan while the bytes that reach the endpoint differ -- anything the
route appends after compilation, or any prompt the service builds per attempt, is outside it.

This test records the prompt at the PORT BOUNDARY, which is the last place the text exists before it
leaves the process, and asserts the two paths agree there. A divergence anywhere in between fails it,
without anyone having to guess which step introduced it.

THE INJECTION GAP THIS ALSO CLOSES
----------------------------------
`generation/routes.py` appends this project's learned preferences to the compiled text after compilation.
`test_prompt_requirements_achievable.py` audits the ASSEMBLED prompt for a literal that steers the model
-- a Python image for a Node repository, the defect that cost a journey run -- but it audits what
`compile_prompt` returns, so anything appended afterwards is invisible to it. A stored preference naming an
image or a version would steer 7b exactly as `python:3.13-slim` did and nothing would flag it. The audit
below runs over the SENT text, so the boundary the assertion covers is the boundary the model sees.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.auth.dependencies import require_principal
from src.auth.models import UserRole
from src.auth.principal import Principal
from src.core.model_port import ModelCompletion
from src.generation.service import GenerationOutcome, GenerationService

from tests.integration.production_app import real_app_lifespan

pytestmark = [pytest.mark.asyncio, pytest.mark.mandatory]

GENERATION_PATH = "/api/v1/generation/runs"

# The criterion-10 fixture's shape: a Node service that ALREADY HAS a workflow, a chart and terraform.
# That is the detail the harness's own fixture lacked, and lacking it is why the harness passed while the
# journey did not.
FIXTURE: dict[str, str] = {
    "server.js": "const http = require('http');\nhttp.createServer((q, r) => r.end('ok')).listen(3000);\n",
    "package.json": '{\n  "name": "checkout",\n  "main": "server.js"\n}\n',
    "README.md": "# checkout\n",
    ".github/workflows/build.yml": (
        "name: build\non: [push]\njobs:\n  b:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v4\n"
    ),
    "infra/main.tf": 'resource "null_resource" "x" {}\n',
}

PROMPT = "Generate a Dockerfile and Kubernetes manifests for this Node.js service."


def _language_of(path: str) -> str:
    """The language column the scanner would have written. Real values, because `file_contents` is a
    production table and a row with a wrong language is a row production would not have made."""
    suffix = path.rsplit(".", 1)[-1].lower()
    return {
        "js": "javascript",
        "json": "json",
        "md": "markdown",
        "yml": "yaml",
        "yaml": "yaml",
        "tf": "hcl",
    }.get(suffix, "text")


class _Recorder:
    """Records every prompt it is handed and refuses to produce content.

    Refusing is deliberate: this file is about the TEXT SENT, and a model that answered would spend
    minutes and add a second variable. The prompts are recorded before the refusal, so both paths are
    compared on exactly what they asked for.
    """

    tier_name = "self_hosted"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(  # noqa: ANN201
        self,
        *,
        prompt,
        on_token=None,
        may_serve_from_cache=True,
        store_in_cache=True,  # noqa: ANN001
    ):
        self.prompts.append(str(prompt))
        return ModelCompletion(ok=False, failure_reasons=("recorder: no content by design",))

    async def remember(self, *, prompt, content) -> None:  # noqa: ANN001
        return None


@pytest_asyncio.fixture
async def indexed_project(head_engine: AsyncEngine) -> Any:
    """A real project with the fixture indexed, because the route reads its plan from the database.

    Rows rather than a stubbed evidence object: the route's `_compile_generation_plan` queries
    `file_tree` and `file_contents`, and a stub there would test a path production does not have.
    """
    maker = async_sessionmaker(head_engine, expire_on_commit=False)
    pid = uuid.uuid4()
    uid = uuid.uuid4()
    async with maker() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, name, role, idp_subject, is_active) "
                "VALUES (:id, :email, 'Bytes Operator', 'admin', :sub, true)"
            ),
            {"id": uid, "email": f"bytes-{uid.hex[:8]}@example.invalid", "sub": f"sub-{uid.hex}"},
        )
        await session.execute(
            text("INSERT INTO projects (id, name, path) VALUES (:id, :n, :p)"),
            {"id": pid, "n": f"bytes-{pid.hex[:8]}", "p": "/workspace"},
        )
        for path, body in FIXTURE.items():
            file_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO file_tree (id, project_id, path, content_hash, size_bytes, "
                    "last_modified, created_at) VALUES (:id, :p, :path, :h, :s, now(), now())"
                ),
                {"id": file_id, "p": pid, "path": path, "h": f"sha256:{file_id.hex}", "s": len(body)},
            )
            await session.execute(
                text(
                    "INSERT INTO file_contents (file_id, content, language, redaction_count, "
                    "updated_at) VALUES (:f, :c, :lang, 0, now())"
                ),
                {"f": file_id, "c": body, "lang": _language_of(path)},
            )
        # THE INVENTORY, which is where the route reads the project's facts from. Without an
        # `analysis_reports` row the inventory is `{}` -- and then the Dockerfile's base-image
        # instruction SILENTLY VANISHES, because it is resolved from the detected language. The prompt
        # still says 'match the language of THIS repository' and names no image, which is exactly the
        # condition under which 7b wrote a Python build for a Node project.
        #
        # The values are the ones the REAL scan produced for this fixture, read back from the live
        # stack: `hcl` first and `javascript` second (so the base-image lookup must skip an unmapped
        # language rather than give up at the first one), and NO package managers. Inventing `npm` here
        # is what made an earlier reproduction disagree with production in one detail.
        await session.execute(
            text(
                "INSERT INTO analysis_reports "
                "(id, project_id, score, categories, inventory_hash, report_version, inventory) "
                "VALUES (:id, :p, 28, :cats, :hash, 1, :inv)"
            ),
            {
                "id": uuid.uuid4(),
                "p": pid,
                "cats": json.dumps({"containerization_score": 0}),
                "hash": f"sha256:{pid.hex}",
                "inv": json.dumps(
                    {
                        "languages": ["hcl", "javascript", "yaml"],
                        "package_managers": [],
                        "entry_points": ["server.js"],
                        "file_count": len(FIXTURE),
                    }
                ),
            },
        )
        await session.commit()
    try:
        yield pid, uid
    finally:
        async with maker() as session:
            await session.execute(
                text("DELETE FROM file_contents WHERE file_id IN (SELECT id FROM file_tree WHERE project_id = :p)"),
                {"p": pid},
            )
            await session.execute(text("DELETE FROM file_tree WHERE project_id = :p"), {"p": pid})
            await session.execute(text("DELETE FROM generation_runs WHERE project_id = :p"), {"p": pid})
            await session.execute(text("DELETE FROM projects WHERE id = :p"), {"p": pid})
            await session.execute(text("DELETE FROM analysis_reports WHERE project_id = :p"), {"p": pid})
            await session.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await session.commit()


async def _route_prompts(
    monkeypatch: pytest.MonkeyPatch, project_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[list[str], Any]:
    """Drive the REAL route and return the prompts the port received."""
    from src.main import create_app

    # THE AMBIENT ENVIRONMENT, not the committed baseline. `apply_committed_baseline_env` sets the
    # container hostnames from `.env.example`, and `production_app` deliberately points the database at
    # a closed port -- both are right for a wiring test and wrong here, because this route READS THE
    # PROJECT'S INDEX from the database. Driving it against unreachable services would test the
    # error path and call it a prompt comparison.
    monkeypatch.setenv("APP_ENV", "test")
    # The committed tier file refuses an unexpanded variable, and it is right to: an endpoint whose
    # base URL is a literal `${OPENAI_BASE_URL}` would fail at call time with a DNS error instead of at
    # load time with the name of the missing setting. These are never called -- the port is replaced by
    # a recorder -- but the tier must LOAD for the app to compose.
    for name, value in (
        ("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        ("XAI_BASE_URL", "https://api.x.ai/v1"),
        ("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        ("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        ("GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com"),
        ("SELF_HOSTED_BASE_URL", "http://localhost:11434/v1"),
        ("SELF_HOSTED_SECONDARY_BASE_URL", "http://localhost:11434/v1"),
        ("SELF_HOSTED_MODEL_ID", "qwen2.5-coder:7b"),
    ):
        monkeypatch.setenv(name, os.environ.get(name) or value)
    app = create_app()
    async with real_app_lifespan(app):
        recorder = _Recorder()
        app.state.artifact_model = recorder
        # ONLY `require_principal` is satisfied, which is this suite's convention: everything else on
        # the route -- the plan compile, the injection, the persistence -- stays production code.
        app.dependency_overrides[require_principal] = lambda: Principal.for_user(
            user_id=user_id,
            subject="sent-bytes",
            email="bytes@example.invalid",
            role=UserRole.ADMIN,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            response = await client.post(
                GENERATION_PATH,
                json={"project_id": str(project_id), "prompt": PROMPT},
            )
            # The body is consumed so the generator runs to completion; its content is not the subject.
            await response.aread()
        return recorder.prompts, app


async def _service_prompts(session: AsyncSession, project_id: uuid.UUID) -> list[str]:
    """Drive the SERVICE directly, compiling the plan the same way the route does."""
    from src.generation.routes import _compile_generation_plan

    compiled, existing, repairs = await _compile_generation_plan(
        session, project_id=project_id, selected_check_ids=None
    )
    recorder = _Recorder()
    outcome = GenerationOutcome(run_id=uuid.uuid4())
    async for _ in GenerationService(model=recorder).stream_generation(
        project_id,
        PROMPT,
        outcome=outcome,
        project={"name": "checkout", "settings": {}},
        compiled=compiled,
        existing=existing,
        repair_prompts=repairs,
    ):
        pass
    return recorder.prompts


class TestBothPathsSendTheSameBytes:
    async def test_the_first_prompt_is_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch, indexed_project: tuple[uuid.UUID, uuid.UUID], head_engine: AsyncEngine
    ) -> None:
        """THE DIVERGENCE, asserted where it matters.

        A difference here is the whole explanation for a route that falls back while the service is
        accepted, and the assertion names the first differing line rather than reporting inequality.
        """
        project_id, user_id = indexed_project
        route, _ = await _route_prompts(monkeypatch, project_id, user_id)
        maker = async_sessionmaker(head_engine, expire_on_commit=False)
        async with maker() as session:
            service = await _service_prompts(session, project_id)

        assert route, "the route never called the model"
        assert service, "the service never called the model"

        if route[0] != service[0]:
            import difflib

            diff = list(
                difflib.unified_diff(
                    service[0].splitlines(), route[0].splitlines(), "service", "route", lineterm="", n=1
                )
            )
            pytest.fail("the route and the service send different bytes:\n" + "\n".join(diff[:40]))

    async def test_neither_path_appends_a_foreign_toolchain_literal(
        self, monkeypatch: pytest.MonkeyPatch, indexed_project: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """THE AUDIT, MOVED TO THE SENT TEXT.

        `test_prompt_requirements_achievable.py` checks what `compile_prompt` returns. The route appends
        learned preferences AFTER that, so a preference naming an image or a version would steer the model
        exactly as `python:3.13-slim` did and no audit would see it. An audit that stops one step short of
        the boundary has a gap by construction.
        """
        project_id, user_id = indexed_project
        route, _ = await _route_prompts(monkeypatch, project_id, user_id)

        sent = route[0]
        foreign = [t for t in ("python:", "pip install", "requirements.txt", "uvicorn") if t in sent]
        assert foreign == [], (
            f"the text SENT to the model offers a Node repository {foreign}; this is the "
            f"`python:3.13-slim` defect arriving by a route the compile-time audit cannot see"
        )
        # NON-VACUITY: the prompt must still name this repository's own toolchain.
        assert "node:" in sent, "the sent prompt names no Node base image at all"

    async def test_no_write_target_in_the_sent_text_is_a_directory(
        self, monkeypatch: pytest.MonkeyPatch, indexed_project: tuple[uuid.UUID, uuid.UUID]
    ) -> None:
        """The fixture HAS a workflows directory, which is the condition that produced a directory write
        target and made every run on such a repository fall through to the template."""
        project_id, user_id = indexed_project
        route, _ = await _route_prompts(monkeypatch, project_id, user_id)

        # Paths the prompt instructs a write to are quoted after `CREATE` or `MODIFY`.
        targets = re.findall(r"### \d+\.\d+ (?:CREATE|MODIFY) `([^`]+)`", route[0])
        assert targets, "the sent prompt names no write targets at all"
        assert [t for t in targets if t.endswith("/")] == [], targets
