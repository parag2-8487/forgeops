# SPDX-License-Identifier: FSL-1.1-ALv2
"""Shared fixtures for the backend integration tests.

These tests deliberately require REAL services. design.md §7.6 is explicit that
backend integration tests run "against the real app and a real Postgres/Redis" —
substituting SQLite or a fake would not exercise the pgvector extension, the
HNSW index, or the readiness contract at all.

A live PostgreSQL with the `vector` extension available is selected through
`FORGEOPS_TEST_DATABASE_URL`, e.g.

    postgresql+asyncpg://postgres@127.0.0.1:55432/forgeops_test

When that variable is unset the schema tests are skipped with an explicit
reason, so a run without a database never silently reports success.
"""

from __future__ import annotations

import os

import pytest

from .capability import require_capability

# The chokepoint transit fixtures, re-exported for the same reason: a test module that
# imported them by NAME would shadow its own methods' parameters of the same name, and
# `sessions`, `redis_client` and `sink` appear in almost every signature in
# `test_governance_chokepoint.py`. Through conftest they are discovered rather than imported.
from .chokepoint_support import redis_client, redis_url, sessions, sink  # noqa: F401

# Re-exported for the same reason: pytest only discovers fixtures from conftest or
# from a plugin, and the §6.5 revision proofs (test_0002 … test_0009) all need the
# same session-scoped "schema at head" and rolled-back-transaction fixtures. They
# live in migration_support.py because that file carries the reasoning for the two
# choices behind them — Alembic in a subprocess, and a read-only shared schema.
from .migration_support import (  # noqa: F401
    conn,
    head_engine,
    schema_at_head,
)

# Re-exported so every integration test can request the app-factory-derived
# fixture without importing the module (design.md §0.4.1). Defined in its own file
# because that file carries the rule the fixture enforces: it may substitute a
# transport, never a collaborator.
from .production_app import composed_state_attributes, production_app  # noqa: F401

TEST_DATABASE_URL_ENV = "FORGEOPS_TEST_DATABASE_URL"


def _require_database_url() -> str:
    url = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not url:
        require_capability(
            "postgres",
            f"{TEST_DATABASE_URL_ENV} is not set; these tests require a real "
            "PostgreSQL 17 with the pgvector extension available "
            "(design.md §7.6)",
        )
    return url


@pytest.fixture(scope="session")
def database_url() -> str:
    """Async SQLAlchemy URL for the real test database."""
    return _require_database_url()


@pytest.fixture(scope="session")
def sync_database_url(database_url: str) -> str:
    """psycopg-free synchronous form, used only for URL assertions."""
    return database_url.replace("+asyncpg", "")
