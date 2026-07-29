# SPDX-License-Identifier: FSL-1.1-ALv2
"""Redis Tasks Extension state machine (Design §12.4).

Implements the MCP Tasks Extension lifecycle with Redis-backed persistence.
Idempotent cancellation: terminal state → no error, returns existing state.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class TaskState(StrEnum):
    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL: frozenset[TaskState] = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELLED,
    }
)

ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    TaskState.SUBMITTED: frozenset({TaskState.WORKING, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.WORKING: frozenset(
        {
            TaskState.INPUT_REQUIRED,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.INPUT_REQUIRED: frozenset({TaskState.WORKING, TaskState.CANCELLED, TaskState.FAILED}),
    # Terminal states allow no transitions
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


def can_transition(from_state: TaskState, to_state: TaskState) -> bool:
    """Check whether a state transition is allowed."""
    return to_state in ALLOWED.get(from_state, frozenset())


@dataclass
class TaskRecord:
    """A single task's persisted state."""

    task_id: str
    state: TaskState
    kind: str
    owner: str = "default"
    arguments: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_json(self) -> str:
        """Serialize to JSON for Redis storage."""
        d = asdict(self)
        d["state"] = self.state.value
        return json.dumps(d)

    @classmethod
    def from_json(cls, data: str) -> TaskRecord:
        """Deserialize from Redis JSON."""
        d = json.loads(data)
        d["state"] = TaskState(d["state"])
        return cls(**d)


class TaskConflictError(RuntimeError):
    """A compare-and-set lost: the task changed state concurrently (P-10)."""


class RedisForTasks(Protocol):
    """Minimal async Redis interface for the task store."""

    async def set(self, name: str, value: str, px: int | None = None) -> Any: ...
    async def get(self, name: str) -> str | None: ...
    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...


# Compare-and-set transition. The read, the state comparison and the write happen
# inside one Redis EVAL, so two concurrent tasks/update calls cannot both win
# (P-10). Design §11.5 describes WATCH/MULTI; a server-side script gives the same
# guarantee without a client retry loop, and cannot be defeated by a fake that
# emulates optimistic locking incorrectly.
#
#   KEYS[1] = task key
#   ARGV[1] = expected current state
#   ARGV[2] = new serialised record
#   ARGV[3] = TTL in milliseconds
# Returns 1 on success, 0 when the state no longer matches, -1 when absent.
CAS_TRANSITION_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then
    return -1
end
local record = cjson.decode(current)
if record.state ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'PX', tonumber(ARGV[3]))
return 1
"""


class RedisTaskStore:
    """Redis-backed task store with state machine enforcement.

    - Idempotent cancellation: cancelling a terminal task returns existing state.
    - Invalid transitions raise ValueError.
    """

    KEY_PREFIX = "mcp:task:"
    DEFAULT_TTL_MS = 86_400_000  # 24 hours

    def __init__(self, redis: RedisForTasks, *, ttl_ms: int | None = None) -> None:
        self._redis = redis
        self._ttl_ms = ttl_ms or self.DEFAULT_TTL_MS

    def _key(self, task_id: str) -> str:
        return f"{self.KEY_PREFIX}{task_id}"

    async def create(self, *, kind: str, owner: str) -> TaskRecord:
        """Create a new task in SUBMITTED state (design §11.5)."""
        now = time.time()
        record = TaskRecord(
            task_id=str(uuid.uuid4()),
            state=TaskState.SUBMITTED,
            kind=kind,
            owner=owner,
            created_at=now,
            updated_at=now,
        )
        await self._redis.set(self._key(record.task_id), record.to_json(), px=self._ttl_ms)
        return record

    async def get(self, task_id: str) -> TaskRecord | None:
        """Retrieve a task by ID. Returns None if not found."""
        data = await self._redis.get(self._key(task_id))
        if data is None:
            return None
        return TaskRecord.from_json(data)

    async def update(
        self,
        task_id: str,
        new_state: TaskState,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> TaskRecord:
        """Transition a task to a new state under compare-and-set.

        Raises ValueError if the task is absent or the transition is not allowed,
        and TaskConflictError when another writer changed the state first (P-10).
        """
        record = await self.get(task_id)
        if record is None:
            raise ValueError(f"Task {task_id} not found")

        # The HTTP surface supplies a raw string; normalise before comparing or
        # serialising, so an unknown state is a clean ValueError rather than an
        # AttributeError deep inside to_json().
        try:
            new_state = TaskState(new_state)
        except ValueError as exc:
            raise ValueError(f"Unknown task state: {new_state}") from exc

        if not can_transition(record.state, new_state):
            raise ValueError(f"Invalid transition: {record.state.value} → {new_state.value}")

        expected = record.state
        record.state = new_state
        record.updated_at = time.time()
        if result is not None:
            record.result = result
        if error is not None:
            record.error = error

        await self._commit(task_id, expected, record)
        return record

    async def cancel(self, task_id: str) -> TaskRecord:
        """Cancel a task. Idempotent: a terminal task is returned unchanged.

        Raises ValueError if the task is not found.
        """
        record = await self.get(task_id)
        if record is None:
            raise ValueError(f"Task {task_id} not found")

        # Idempotent: already in a terminal state → return as-is
        if record.state in TERMINAL:
            return record

        expected = record.state
        record.state = TaskState.CANCELLED
        record.updated_at = time.time()
        await self._commit(task_id, expected, record)
        return record

    async def _commit(self, task_id: str, expected: TaskState, record: TaskRecord) -> None:
        """Write `record` only while Redis still holds `expected` as the state."""
        outcome = await self._redis.eval(
            CAS_TRANSITION_LUA,
            1,
            self._key(task_id),
            expected.value,
            record.to_json(),
            str(self._ttl_ms),
        )
        code = int(outcome)
        if code == 1:
            return
        if code == -1:
            raise ValueError(f"Task {task_id} not found")
        raise TaskConflictError(f"Task {task_id} changed state concurrently; expected {expected.value}")
