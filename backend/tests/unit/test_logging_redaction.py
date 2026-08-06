# SPDX-License-Identifier: FSL-1.1-ALv2
"""Secret redaction must reach exception tracebacks, not only log messages.

`SecretRedactingFilter` rewrote `record.msg` and `record.args`, but
`JSONFormatter.format` wrote `self.formatException(record.exc_info)` — the
inherited, unredacted traceback — straight into the `exception` field. That is the
single most likely path for a credential to reach a log: `asyncpg`, `sqlalchemy`
and `httpx` all put the URL in the exception message, and connection URLs carry
the password.

Every literal below is synthetic and self-labelling, per
`.antigravity/steering/secret-safety.md`. None resembles a real provider token.

Design authority: §7.2, §14.4 ("the SecretRedactingFilter runs before any handler
emits"), and P-09's secret clause.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from src.core.logging import (
    ConsoleFormatter,
    JSONFormatter,
    SecretRedactingFilter,
    redact_secrets,
)

from tests.synthetic_secrets import (
    SYNTHETIC_MARKER,
    bearer_clause,
    pem_header,
    postgres_dsn,
    redis_dsn,
)

# Assembled at runtime, not written as literals — see tests/synthetic_secrets.py
# for why a credential-shaped source literal is itself a problem.
FAKE_DSN = postgres_dsn()
FAKE_REDIS_DSN = redis_dsn()
FAKE_BEARER = bearer_clause()
FAKE_PASSWORD = SYNTHETIC_MARKER

SECRET_SUBSTRINGS = (FAKE_PASSWORD, "postgresql+asyncpg://", "redis://", "Bearer " + SYNTHETIC_MARKER[:9])


def _emit(formatter: logging.Formatter, exc: BaseException, *, with_filter: bool = True) -> str:
    """Log `exc` through a real handler and return exactly what was written."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    if with_filter:
        handler.addFilter(SecretRedactingFilter())

    logger = logging.getLogger(f"redaction-test-{id(exc)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.ERROR)

    try:
        raise exc
    except BaseException:
        logger.exception("request failed")

    handler.flush()
    return stream.getvalue()


def _assert_clean(output: str) -> None:
    assert "[REDACTED]" in output, f"nothing was redacted in: {output[:400]}"
    for secret in SECRET_SUBSTRINGS:
        assert secret not in output, f"{secret!r} leaked into the log output"


class TestJsonFormatterRedactsTracebacks:
    def test_a_dsn_in_the_exception_message_is_redacted(self):
        output = _emit(JSONFormatter(), RuntimeError(f"Connection failed: {FAKE_DSN}"))

        entry = json.loads(output)
        assert "exception" in entry, "the traceback was not emitted at all"
        _assert_clean(output)

    def test_a_bearer_clause_in_the_exception_message_is_redacted(self):
        output = _emit(JSONFormatter(), RuntimeError(f"upstream rejected {FAKE_BEARER}"))

        _assert_clean(output)

    def test_a_redis_dsn_in_a_chained_cause_is_redacted(self):
        cause = OSError(f"could not connect to {FAKE_REDIS_DSN}")
        exc = RuntimeError("readiness probe failed")
        exc.__cause__ = cause

        output = _emit(JSONFormatter(), exc)

        _assert_clean(output)

    def test_the_log_message_itself_is_still_redacted(self):
        """The pre-existing guarantee must not regress."""
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JSONFormatter())
        handler.addFilter(SecretRedactingFilter())

        logger = logging.getLogger("redaction-test-msg")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.ERROR)
        logger.error("connecting to %s", FAKE_DSN)
        handler.flush()

        _assert_clean(stream.getvalue())


class TestConsoleFormatterRedactsTracebacks:
    def test_console_output_is_not_a_hole(self):
        """LOG_FORMAT=console must not leak what LOG_FORMAT=json redacts."""
        formatter = ConsoleFormatter("%(levelname)s %(name)s: %(message)s")

        output = _emit(formatter, RuntimeError(f"Connection failed: {FAKE_DSN}"))

        _assert_clean(output)


class TestExcTextIsScrubbed:
    def test_a_precomputed_exc_text_is_redacted_by_the_filter(self):
        """Some handlers cache the traceback on the record before emission."""
        record = logging.LogRecord(
            name="t", level=logging.ERROR, pathname=__file__, lineno=1, msg="failed", args=(), exc_info=None
        )
        record.exc_text = f"Traceback...\nRuntimeError: {FAKE_DSN}"

        SecretRedactingFilter().filter(record)

        assert FAKE_PASSWORD not in record.exc_text
        assert "[REDACTED]" in record.exc_text


class TestRedactionIsNonVacuous:
    """Guard against the patterns silently ceasing to match."""

    @pytest.mark.parametrize(
        "text",
        [
            FAKE_DSN,
            FAKE_REDIS_DSN,
            FAKE_BEARER,
            pem_header(),
        ],
    )
    def test_each_pattern_family_is_matched(self, text: str):
        assert redact_secrets(text) != text
        assert "[REDACTED]" in redact_secrets(text)

    def test_ordinary_text_is_left_alone(self):
        benign = "plan complete: 3 to add, 0 to change, 0 to destroy"
        assert redact_secrets(benign) == benign
