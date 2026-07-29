# SPDX-License-Identifier: FSL-1.1-ALv2
"""Per-endpoint circuit breaker (Design §13.3).

Implements a three-state breaker: closed → open → half_open → closed.
- Threshold: 5 failures within 30s window → open.
- Cooldown: 60s in open state before transitioning to half_open.
- Half-open: one probe allowed; success → closed, failure → open.
- Clock injection for deterministic testing.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from enum import StrEnum


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-endpoint circuit breaker with injectable clock."""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        failure_window_seconds: float = 30.0,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._failure_window = failure_window_seconds
        self._cooldown = cooldown_seconds
        self._clock = clock or time.monotonic

        self._state = BreakerState.CLOSED
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        self._half_open_probe_active: bool = False

    def state(self) -> BreakerState:
        """Current breaker state, with automatic open → half_open transition."""
        if self._state == BreakerState.OPEN:
            now = self._clock()
            if self._opened_at is not None and (now - self._opened_at) >= self._cooldown:
                self._state = BreakerState.HALF_OPEN
                self._half_open_probe_active = False
        return self._state

    def allows(self) -> bool:
        """Whether the breaker permits a request.

        - CLOSED: always allows.
        - OPEN: never allows (until cooldown transitions to half_open).
        - HALF_OPEN: allows exactly one probe request.
        """
        current = self.state()
        if current == BreakerState.CLOSED:
            return True
        if current == BreakerState.OPEN:
            return False
        # HALF_OPEN — allow one probe
        if not self._half_open_probe_active:
            self._half_open_probe_active = True
            return True
        return False

    def record_success(self) -> None:
        """Record a successful request. Resets breaker from half_open to closed."""
        current = self.state()
        if current == BreakerState.HALF_OPEN:
            self._state = BreakerState.CLOSED
            self._failures.clear()
            self._opened_at = None
            self._half_open_probe_active = False
        elif current == BreakerState.CLOSED:
            # Success in closed state — no action needed
            pass

    def record_failure(self) -> None:
        """Record a failed request. May trip the breaker."""
        current = self.state()
        now = self._clock()

        if current == BreakerState.HALF_OPEN:
            # Probe failed — back to open
            self._state = BreakerState.OPEN
            self._opened_at = now
            self._half_open_probe_active = False
            return

        if current == BreakerState.CLOSED:
            self._failures.append(now)
            # Evict failures outside the window
            cutoff = now - self._failure_window
            while self._failures and self._failures[0] < cutoff:
                self._failures.popleft()
            # Check threshold
            if len(self._failures) >= self._failure_threshold:
                self._state = BreakerState.OPEN
                self._opened_at = now
                self._failures.clear()
