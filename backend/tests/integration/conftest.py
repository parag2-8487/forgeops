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

TEST_DATABASE_URL_ENV = "FORGEOPS_TEST_DATABASE_URL"


def _require_database_url() -> str:
    url = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not url:
        require_capability(
            f"{TEST_DATABASE_URL_ENV} is not set; these tests require a real "
            "PostgreSQL 17 with the pgvector extension available "
            "(design.md §7.6)"
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
