# SPDX-License-Identifier: FSL-1.1-ALv2
"""Property test P-01: Stateful circuit breaker verification.

Uses hypothesis RuleBasedStateMachine to generate sequences of success/failure/tick
events and prove:
- Only valid states (closed, open, half_open)
- Threshold-within-window opening
- Cooldown-only half-open transition
- One probe allowed in half-open
- Success resets to closed
- No spontaneous state changes
"""

from __future__ import annotations

from hypothesis import settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)
from src.ai.routing.breaker import BreakerState, CircuitBreaker


class CircuitBreakerMachine(RuleBasedStateMachine):
    """Stateful property test for CircuitBreaker."""

    def __init__(self):
        super().__init__()
        self._time = 0.0
        self._breaker: CircuitBreaker | None = None
        # Model state
        self._model_state = BreakerState.CLOSED
        self._model_failures: list[float] = []
        self._model_opened_at: float | None = None
        self._model_probe_active: bool = False
        # Config
        self._threshold = 5
        self._window = 30.0
        self._cooldown = 60.0

    @initialize()
    def init_breaker(self):
        self._time = 0.0
        self._breaker = CircuitBreaker(
            failure_threshold=self._threshold,
            failure_window_seconds=self._window,
            cooldown_seconds=self._cooldown,
            clock=lambda: self._time,
        )
        self._model_state = BreakerState.CLOSED
        self._model_failures = []
        self._model_opened_at = None
        self._model_probe_active = False

    @rule()
    def record_success(self):
        """Record a success event."""
        # Update model
        if self._model_state == BreakerState.HALF_OPEN:
            self._model_state = BreakerState.CLOSED
            self._model_failures = []
            self._model_opened_at = None
            self._model_probe_active = False

        # Act on real breaker
        self._breaker.record_success()

    @rule()
    def record_failure(self):
        """Record a failure event."""
        # Update model first
        if self._model_state == BreakerState.HALF_OPEN:
            self._model_state = BreakerState.OPEN
            self._model_opened_at = self._time
            self._model_probe_active = False
        elif self._model_state == BreakerState.CLOSED:
            self._model_failures.append(self._time)
            # Evict old failures
            cutoff = self._time - self._window
            self._model_failures = [t for t in self._model_failures if t >= cutoff]
            if len(self._model_failures) >= self._threshold:
                self._model_state = BreakerState.OPEN
                self._model_opened_at = self._time
                self._model_failures = []

        # Act on real breaker
        self._breaker.record_failure()

    @rule()
    def tick_small(self):
        """Advance time by a small amount (not enough to trigger transitions)."""
        self._time += 5.0
        # Model: check if open → half_open transition
        self._maybe_transition_to_half_open()

    @rule()
    def tick_past_cooldown(self):
        """Advance time past the cooldown period."""
        self._time += self._cooldown + 1.0
        self._maybe_transition_to_half_open()

    @rule()
    def tick_past_window(self):
        """Advance time past the failure window."""
        self._time += self._window + 1.0
        self._maybe_transition_to_half_open()

    @rule()
    def check_allows(self):
        """Check allows() and verify model consistency."""
        # In our model, compute what allows should return
        self._maybe_transition_to_half_open()

        if self._model_state == BreakerState.CLOSED:
            expected_allows = True
        elif self._model_state == BreakerState.OPEN:
            expected_allows = False
        else:
            # HALF_OPEN: allows one probe
            if not self._model_probe_active:
                expected_allows = True
                self._model_probe_active = True
            else:
                expected_allows = False

        actual_allows = self._breaker.allows()
        assert actual_allows == expected_allows

    def _maybe_transition_to_half_open(self):
        """Check if model should transition open → half_open."""
        if self._model_state == BreakerState.OPEN and self._model_opened_at is not None:
            if (self._time - self._model_opened_at) >= self._cooldown:
                self._model_state = BreakerState.HALF_OPEN
                self._model_probe_active = False

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------

    @invariant()
    def state_is_valid(self):
        """State is always one of the three valid states."""
        state = self._breaker.state()
        assert state in (BreakerState.CLOSED, BreakerState.OPEN, BreakerState.HALF_OPEN)

    @invariant()
    def model_matches_actual(self):
        """Model state matches actual breaker state."""
        self._maybe_transition_to_half_open()
        actual = self._breaker.state()
        assert actual == self._model_state, f"Model says {self._model_state} but breaker says {actual}"


# Hypothesis will auto-discover this
TestCircuitBreakerStateful = CircuitBreakerMachine.TestCase
TestCircuitBreakerStateful.settings = settings(
    max_examples=200,
    stateful_step_count=30,
    deadline=None,
)


class TestBreakerThresholdWithinWindow:
    """Prove threshold-within-window opening."""

    def test_exactly_at_threshold(self):
        """Exactly threshold failures within window opens breaker."""
        t = 0.0
        breaker = CircuitBreaker(
            failure_threshold=5,
            failure_window_seconds=30.0,
            cooldown_seconds=60.0,
            clock=lambda: t,
        )

        assert breaker.state() == BreakerState.CLOSED
        for _ in range(4):
            breaker.record_failure()
            assert breaker.state() == BreakerState.CLOSED

        breaker.record_failure()
        assert breaker.state() == BreakerState.OPEN

    def test_failures_outside_window_dont_trip(self):
        """Failures spread beyond the window don't trip the breaker."""
        t = 0.0
        breaker = CircuitBreaker(
            failure_threshold=5,
            failure_window_seconds=30.0,
            cooldown_seconds=60.0,
            clock=lambda: t,
        )

        # 3 failures at t=0
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state() == BreakerState.CLOSED

        # Advance past window, then 2 more failures
        t = 31.0
        for _ in range(2):
            breaker.record_failure()
        # Only 2 in current window — should still be closed
        assert breaker.state() == BreakerState.CLOSED


class TestBreakerCooldownOnlyHalfOpen:
    """Prove cooldown-only transition to half_open."""

    def test_open_stays_until_cooldown(self):
        """Open breaker doesn't become half_open before cooldown."""
        t = 0.0
        breaker = CircuitBreaker(
            failure_threshold=1,
            failure_window_seconds=10.0,
            cooldown_seconds=60.0,
            clock=lambda: t,
        )

        breaker.record_failure()
        assert breaker.state() == BreakerState.OPEN

        # Not enough time
        t = 59.0
        assert breaker.state() == BreakerState.OPEN

        # Exactly at cooldown
        t = 60.0
        assert breaker.state() == BreakerState.HALF_OPEN

    def test_only_one_probe_in_half_open(self):
        """Half-open state allows exactly one probe."""
        t = 0.0
        breaker = CircuitBreaker(
            failure_threshold=1,
            failure_window_seconds=10.0,
            cooldown_seconds=60.0,
            clock=lambda: t,
        )

        breaker.record_failure()
        t = 61.0

        assert breaker.state() == BreakerState.HALF_OPEN
        assert breaker.allows() is True  # first probe
        assert breaker.allows() is False  # no second probe


class TestBreakerSuccessReset:
    """Prove success resets breaker from half_open to closed."""

    def test_success_resets(self):
        t = 0.0
        breaker = CircuitBreaker(
            failure_threshold=1,
            failure_window_seconds=10.0,
            cooldown_seconds=60.0,
            clock=lambda: t,
        )

        breaker.record_failure()
        assert breaker.state() == BreakerState.OPEN

        t = 61.0
        assert breaker.state() == BreakerState.HALF_OPEN
        breaker.record_success()
        assert breaker.state() == BreakerState.CLOSED


class TestBreakerNoSpontaneousChanges:
    """Prove no spontaneous state changes without events or time passage."""

    def test_no_spontaneous(self):
        t = 0.0
        breaker = CircuitBreaker(
            failure_threshold=5,
            failure_window_seconds=30.0,
            cooldown_seconds=60.0,
            clock=lambda: t,
        )

        # Check state many times — should always be closed
        for _ in range(100):
            assert breaker.state() == BreakerState.CLOSED

        # Trip it
        for _ in range(5):
            breaker.record_failure()
        assert breaker.state() == BreakerState.OPEN

        # Check many times without time passage — should stay open
        for _ in range(100):
            assert breaker.state() == BreakerState.OPEN
