# SPDX-License-Identifier: FSL-1.1-ALv2
"""`ArqDispatcher` behind the unchanged seam (design.md §4.6, §7.10, §11.1, D-32).

The seam exists because OQ-16 — Temporal versus Inngest for Phase 2 — is still open.
Every engine concept that reaches business logic is a rewrite when that decision lands,
so these tests are as much about what ARQ must NOT leak as about what it does.

Four things are asserted:

1. the Phase 0 surface (`TaskDispatcher`, `TaskHandle`, `_TASK_HANDLERS`,
   `@register_task`) is untouched;
2. `ArqDispatcher` leaks no engine concept upward — the return type is `TaskHandle` and
   there is no path from it to a job object, a workflow id, a signal or a query;
3. every registered handler produces the SAME result under `InlineDispatcher` and
   `ArqDispatcher`, so swapping dispatchers cannot change behaviour;
4. a duplicate enqueue under one idempotency key is a no-op.

The ARQ pool is substituted by a small real class rather than a Mock: `enqueue_job`'s
signature is the contract under test, and a Mock would accept any call at all — the
D-23 hole. `scripts/check-test-doubles.py` FO-TD002/004 exist for exactly this.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from src.core.tasks import (
    _TASK_HANDLERS,
    ArqDispatcher,
    InlineDispatcher,
    TaskDispatcher,
    TaskHandle,
    build_dispatcher,
    register_task,
)

pytestmark = pytest.mark.mandatory


class RecordingArqPool:
    """A real stand-in for ARQ's pool, with ARQ's actual `enqueue_job` contract.

    Returns None for an id it has already seen, which is what ARQ does — that is the
    behaviour the idempotency test depends on, so the double has to reproduce it rather
    than accept anything.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None, str | None]] = []
        self._seen: set[str] = set()

    async def enqueue_job(
        self,
        function: str,
        *args: Any,
        _job_id: str | None = None,
        _queue_name: str | None = None,
        **kwargs: Any,
    ) -> object | None:
        payload = args[0] if args else {}
        self.calls.append((function, payload, _job_id, _queue_name))
        if _job_id is not None:
            if _job_id in self._seen:
                return None  # ARQ refuses a duplicate job id
            self._seen.add(_job_id)
        return object()


@pytest.fixture()
def isolated_registry() -> Any:
    """Register handlers without leaking them into the rest of the session."""
    saved = dict(_TASK_HANDLERS)
    _TASK_HANDLERS.clear()
    try:
        yield _TASK_HANDLERS
    finally:
        _TASK_HANDLERS.clear()
        _TASK_HANDLERS.update(saved)


class TestThePhase0SurfaceIsUntouched:
    def test_the_protocol_still_declares_exactly_one_method(self) -> None:
        methods = [n for n in vars(TaskDispatcher) if not n.startswith("_")]
        assert methods == ["enqueue"], methods

    def test_the_enqueue_signature_is_unchanged(self) -> None:
        signature = inspect.signature(TaskDispatcher.enqueue)
        assert list(signature.parameters) == ["self", "name", "payload", "idempotency_key"]
        assert signature.parameters["idempotency_key"].kind is inspect.Parameter.KEYWORD_ONLY

    def test_the_handle_is_still_a_frozen_two_field_record(self) -> None:
        handle = TaskHandle(id="x", dispatcher="arq")
        with pytest.raises(FrozenInstanceError):
            handle.id = "y"  # type: ignore[misc]
        assert set(handle.__dataclass_fields__) == {"id", "dispatcher"}

    def test_both_dispatchers_satisfy_the_protocol(self) -> None:
        """The Python analogue of Go's `var _ Iface = (*Impl)(nil)` (§0.4.2).

        Checked structurally rather than with `isinstance`, because `TaskDispatcher` is
        not `@runtime_checkable` and the leaf requires it to stay untouched. Comparing
        signatures is the stronger check anyway: `runtime_checkable` only verifies that
        the attribute NAMES exist, which is the same hole `spec=` leaves on a Mock.
        """
        expected = inspect.signature(TaskDispatcher.enqueue)
        for dispatcher in (InlineDispatcher(), ArqDispatcher(RecordingArqPool())):
            actual = inspect.signature(type(dispatcher).enqueue)
            assert list(actual.parameters) == list(expected.parameters), type(dispatcher).__name__
            for name, parameter in expected.parameters.items():
                assert actual.parameters[name].kind is parameter.kind, (type(dispatcher).__name__, name)


class TestNoEngineConceptLeaksUpward:
    async def test_enqueue_returns_only_a_task_handle(self) -> None:
        pool = RecordingArqPool()
        result = await ArqDispatcher(pool).enqueue("job", {"a": 1})
        assert type(result) is TaskHandle

    async def test_the_handle_exposes_no_engine_object(self) -> None:
        """No job, no workflow id, no signal, no query — nothing to branch on."""
        handle = await ArqDispatcher(RecordingArqPool()).enqueue("job", {})
        for forbidden in ("job", "workflow", "signal", "query", "cancel", "poll", "result"):
            assert not any(forbidden in name.lower() for name in dir(handle)), forbidden

    def test_the_public_surface_names_no_arq_type(self) -> None:
        """A signature carrying `ArqRedis` would put an engine type in the contract
        that `scripts/collect_call_sites.py` reads."""
        rendered = str(inspect.signature(ArqDispatcher.__init__))
        assert "Arq" not in rendered and "Redis" not in rendered, rendered

    def test_arq_is_imported_from_exactly_one_module(self) -> None:
        """The invariant, asserted against the source rather than the lint config.

        The Ruff banned-api rule is the enforcement, but reading its per-file exemptions
        conflates the arq ban with the unrelated cross-domain-import exemptions that
        share the same TID251 code. Scanning `src/**` for the import states the thing
        that actually matters.

        The answer is ONE module, not two. `src/worker.py` was expected to be the
        second, but it turns out to need nothing from ARQ directly — it takes
        `_redis_settings` and `worker_functions` from `core/tasks.py`, so the entire
        engine surface is behind a single file. That is a stronger position than the
        design assumed, and it is asserted rather than left to drift: adding
        `import arq` to the worker would fail here and have to be argued for.
        """
        import ast
        import tomllib
        from pathlib import Path

        src = Path(__file__).resolve().parents[2] / "src"
        importers: set[str] = set()
        for path in src.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                roots: list[str] = []
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    roots = [node.module.split(".")[0]]
                if "arq" in roots:
                    importers.add(path.relative_to(src.parent).as_posix())

        assert importers == {"src/core/tasks.py"}, sorted(importers)

        # And the ban itself is still configured, so a new importer is a build failure
        # rather than only a failing test.
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        banned = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["ruff"]["lint"]["flake8-tidy-imports"][
            "banned-api"
        ]
        assert "arq" in banned, "the arq import ban was removed"


class TestIdentityOfResultsAcrossDispatchers:
    async def test_every_registered_handler_gives_the_same_handle_id(self, isolated_registry: dict) -> None:
        seen: list[str] = []

        @register_task("alpha")
        async def alpha(payload: dict[str, Any]) -> None:
            seen.append(f"alpha:{payload['n']}")

        @register_task("beta")
        async def beta(payload: dict[str, Any]) -> None:
            seen.append(f"beta:{payload['n']}")

        pool = RecordingArqPool()
        inline, arq = InlineDispatcher(), ArqDispatcher(pool)

        for name in sorted(isolated_registry):
            inline_handle = await inline.enqueue(name, {"n": 1}, idempotency_key=f"k-{name}")
            arq_handle = await arq.enqueue(name, {"n": 1}, idempotency_key=f"k-{name}")
            # The id is the caller-visible identity, and it must not depend on which
            # dispatcher is configured — otherwise a retry keyed on it behaves
            # differently in development and production.
            assert inline_handle.id == arq_handle.id == f"k-{name}"
            assert (inline_handle.dispatcher, arq_handle.dispatcher) == ("inline", "arq")

        assert seen == ["alpha:1", "beta:1"], seen
        assert [call[0] for call in pool.calls] == ["alpha", "beta"]

    async def test_the_inline_dispatcher_runs_the_handler_and_arq_defers_it(self, isolated_registry: dict) -> None:
        """The one difference that IS visible, stated so it is deliberate.

        Inline executes in-process; ARQ hands the job to a worker. Everything the
        caller can observe from the return value is identical.
        """
        ran: list[str] = []

        @register_task("gamma")
        async def gamma(payload: dict[str, Any]) -> None:
            ran.append("yes")

        await InlineDispatcher().enqueue("gamma", {})
        assert ran == ["yes"]

        await ArqDispatcher(RecordingArqPool()).enqueue("gamma", {})
        assert ran == ["yes"], "ArqDispatcher executed the handler in-process"

    async def test_an_unregistered_name_is_not_an_error_for_either(self, isolated_registry: dict) -> None:
        """Inline ignores an unknown name; ARQ cannot know at enqueue time.

        Divergence here would mean a typo'd task name fails in development and silently
        queues in production, or the reverse.
        """
        assert (await InlineDispatcher().enqueue("nope", {})).dispatcher == "inline"
        assert (await ArqDispatcher(RecordingArqPool()).enqueue("nope", {})).dispatcher == "arq"


class TestIdempotency:
    async def test_a_duplicate_enqueue_is_a_no_op(self) -> None:
        pool = RecordingArqPool()
        dispatcher = ArqDispatcher(pool)

        first = await dispatcher.enqueue("index.full", {"project": 1}, idempotency_key="run-7")
        second = await dispatcher.enqueue("index.full", {"project": 1}, idempotency_key="run-7")

        assert first.id == second.id == "run-7"
        # ARQ returned None for the second; the caller still gets a usable handle and
        # cannot tell — which is the point of an idempotent enqueue.
        assert len(pool.calls) == 2
        assert pool.calls[0][2] == pool.calls[1][2] == "run-7"

    async def test_the_key_becomes_the_arq_job_id(self) -> None:
        pool = RecordingArqPool()
        await ArqDispatcher(pool).enqueue("t", {}, idempotency_key="the-key")
        assert pool.calls[0][2] == "the-key"

    async def test_without_a_key_each_enqueue_gets_a_distinct_id(self) -> None:
        pool = RecordingArqPool()
        dispatcher = ArqDispatcher(pool)
        ids = {(await dispatcher.enqueue("t", {})).id for _ in range(5)}
        assert len(ids) == 5

    async def test_the_queue_name_is_passed_through(self) -> None:
        pool = RecordingArqPool()
        await ArqDispatcher(pool, queue_name="forgeops").enqueue("t", {})
        assert pool.calls[0][3] == "forgeops"


class TestDispatcherSelection:
    def test_inline_is_selected_by_configuration(self) -> None:
        from src.core.config import Settings

        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            redis_url="redis://localhost:6379/0",
            task_dispatcher="inline",
        )
        assert isinstance(build_dispatcher(settings), InlineDispatcher)

    def test_arq_is_selected_by_configuration(self) -> None:
        from src.core.config import Settings

        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            redis_url="redis://localhost:6379/0",
            task_dispatcher="arq",
        )
        assert isinstance(build_dispatcher(settings, RecordingArqPool()), ArqDispatcher)

    def test_arq_without_a_pool_is_refused_loudly(self) -> None:
        """Falling back to inline would make a misconfigured production deployment run
        every job in the request thread, which looks like it works."""
        from src.core.config import Settings

        settings = Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            redis_url="redis://localhost:6379/0",
            task_dispatcher="arq",
        )
        with pytest.raises(ValueError, match="requires an ARQ pool"):
            build_dispatcher(settings, None)


class TestTheWorkerExposesEveryHandler:
    """`src/worker.py` reads Settings in its class body, so these tests supply an
    environment before importing it — the worker genuinely requires configuration to
    exist, and a lazily-configured worker would defer a misconfiguration to the first
    job instead of to startup."""

    @staticmethod
    def _worker_settings(monkeypatch: pytest.MonkeyPatch) -> Any:
        import importlib
        import sys

        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
        monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
        sys.modules.pop("src.worker", None)
        return importlib.import_module("src.worker").WorkerSettings

    def test_the_worker_function_set_equals_the_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A handler registered but not imported by the worker is an idle queue.

        `@register_task` populates the registry as an import side effect, so a worker
        that never imports a module handles none of its jobs — and starts successfully
        while doing it.
        """
        settings = self._worker_settings(monkeypatch)
        names = {f.name for f in settings.functions}
        assert names == set(_TASK_HANDLERS), sorted(names ^ set(_TASK_HANDLERS))

    def test_the_worker_reads_the_same_settings_as_the_api(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.core.config import get_settings

        worker = self._worker_settings(monkeypatch)
        settings = get_settings()
        assert worker.queue_name == settings.arq_queue_name
        assert worker.max_jobs == settings.arq_max_jobs
        assert worker.job_timeout == settings.arq_job_timeout_seconds
