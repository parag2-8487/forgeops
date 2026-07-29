# SPDX-License-Identifier: FSL-1.1-ALv2
"""P-10's concurrency clause: two concurrent updates cannot both succeed.

`RedisTaskStore.update` was a read-modify-write: GET, validate the transition,
SET. Two callers could both read `submitted`, both consider their transition
legal, and both write — so a task could be completed and failed, or cancelled
twice. The state machine tests could not see it because they were sequential.

Transitions now go through `CAS_TRANSITION_LUA`, which re-reads the record inside
Redis and writes only while the stored state still equals the state the caller
read. The tests below force the exact interleaving that used to lose a write.

Design authority: §11.5 ("two concurrent tasks/update calls cannot both win").
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from src.mcp.tasks import RedisTaskStore, TaskConflictError, TaskState


class CasRedis:
    """In-memory Redis whose EVAL honours the compare-and-set contract."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.eval_calls = 0

    async def set(self, name: str, value: str, px: int | None = None) -> bool:
        self.values[name] = value
        return True

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        self.eval_calls += 1
        key, expected, new_record, _ttl = keys_and_args[:4]
        current = self.values.get(key)
        if current is None:
            return -1
        if json.loads(current).get("state") != expected:
            return 0
        self.values[key] = new_record
        return 1


@pytest.fixture()
def redis() -> CasRedis:
    return CasRedis()


@pytest.fixture()
def store(redis: CasRedis) -> RedisTaskStore:
    return RedisTaskStore(redis)


pytestmark = pytest.mark.asyncio


async def test_update_goes_through_a_compare_and_set(store: RedisTaskStore, redis: CasRedis):
    """A plain SET would leave eval_calls at zero."""
    record = await store.create(kind="plan", owner="default")

    await store.update(record.task_id, TaskState.WORKING)

    assert redis.eval_calls == 1, "update did not use the compare-and-set path"


async def test_two_concurrent_updates_cannot_both_succeed(store: RedisTaskStore, redis: CasRedis):
    """Both writers read `submitted`; exactly one commit may land.

    Without CAS both `update` calls returned successfully and the second write
    silently overwrote the first, so the task ended in whichever state lost the
    race — the defect this test exists to prevent.
    """
    record = await store.create(kind="plan", owner="default")
    task_id = record.task_id

    # Force the interleaving: both coroutines complete their GET before either
    # commits, which is precisely the window the old implementation left open.
    both_have_read = asyncio.Event()
    reads = 0
    original_get = redis.get

    async def gated_get(name: str) -> str | None:
        nonlocal reads
        value = await original_get(name)
        reads += 1
        if reads >= 2:
            both_have_read.set()
        await both_have_read.wait()
        return value

    redis.get = gated_get  # type: ignore[method-assign]

    results = await asyncio.gather(
        store.update(task_id, TaskState.WORKING),
        store.update(task_id, TaskState.CANCELLED),
        return_exceptions=True,
    )

    succeeded = [r for r in results if not isinstance(r, BaseException)]
    conflicts = [r for r in results if isinstance(r, TaskConflictError)]

    assert len(succeeded) == 1, f"expected exactly one winner, got {results}"
    assert len(conflicts) == 1, f"expected exactly one conflict, got {results}"

    # The stored state must be the winner's, never a blend or the loser's.
    stored = json.loads(redis.values[store._key(task_id)])
    assert stored["state"] == succeeded[0].state.value


async def test_a_conflict_does_not_corrupt_the_record(store: RedisTaskStore, redis: CasRedis):
    """The loser's write is rejected wholesale, not partially applied."""
    record = await store.create(kind="plan", owner="default")
    await store.update(record.task_id, TaskState.WORKING)

    # A stale caller still believing the task is `submitted`.
    stale = await store.get(record.task_id)
    assert stale is not None
    stale.state = TaskState.SUBMITTED

    with pytest.raises(TaskConflictError):
        await store._commit(record.task_id, TaskState.SUBMITTED, stale)

    fresh = await store.get(record.task_id)
    assert fresh is not None
    assert fresh.state is TaskState.WORKING


async def test_cancel_also_commits_under_compare_and_set(store: RedisTaskStore, redis: CasRedis):
    record = await store.create(kind="plan", owner="default")

    cancelled = await store.cancel(record.task_id)

    assert cancelled.state is TaskState.CANCELLED
    assert redis.eval_calls == 1

    # Idempotent: a terminal task short-circuits and issues no further CAS.
    again = await store.cancel(record.task_id)
    assert again.state is TaskState.CANCELLED
    assert redis.eval_calls == 1


async def test_commit_reports_a_vanished_task_as_not_found(store: RedisTaskStore, redis: CasRedis):
    record = await store.create(kind="plan", owner="default")
    redis.values.clear()  # TTL expiry between the read and the commit

    with pytest.raises(ValueError, match="not found"):
        await store._commit(record.task_id, TaskState.SUBMITTED, record)


# ── The same guarantee against a real Redis ─────────────────────────────────
#
# CI sets REDIS_URL for the backend job, so this runs there rather than skipping.
# The DSN is captured at import time on purpose: other modules assign REDIS_URL to
# point at deliberately-closed ports, and reading it at call time made this test
# depend on collection order.
_REDIS_URL_AT_IMPORT = os.environ.get("REDIS_URL", "").strip()


@pytest.mark.asyncio
async def test_cas_holds_against_a_real_redis():
    from tests.integration.capability import require_capability

    url = _REDIS_URL_AT_IMPORT
    if not url:
        require_capability("REDIS_URL is not set; the in-memory CAS tests above still apply")

    redis_asyncio = pytest.importorskip("redis.asyncio")
    client = redis_asyncio.from_url(url, decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        require_capability(f"no Redis reachable at {url}")

    try:
        store = RedisTaskStore(client)
        record = await store.create(kind="plan", owner="default")

        # First transition wins; a second commit from the same stale read loses.
        await store.update(record.task_id, TaskState.WORKING)
        with pytest.raises(TaskConflictError):
            await store._commit(record.task_id, TaskState.SUBMITTED, record)

        fresh = await store.get(record.task_id)
        assert fresh is not None
        assert fresh.state is TaskState.WORKING
        await client.delete(store._key(record.task_id))
    finally:
        await client.aclose()
