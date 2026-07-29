# SPDX-License-Identifier: FSL-1.1-ALv2
"""Async database/session primitives (design.md §6.3–§6.5, §11.3).

- create_db_engine: asyncpg URL, pool_size from settings, max_overflow=5,
  pool_pre_ping, pool_recycle=1800. Construction does NOT require a live database.
- create_sessionmaker: expire_on_commit=False, autoflush=False.
- MetaData naming_convention for deterministic constraint names.
- get_session: request-scoped, commits on success, rolls back on exception.
- with_ef_search: SET LOCAL hnsw.ef_search for transaction-scoped HNSW tuning.

PgBouncer transaction-mode constraint:
  When running behind PgBouncer in transaction mode, asyncpg requires
  connect_args={"statement_cache_size": 0} (or a per-connection prepared-statement
  name salt). Phase 1 adds this when PgBouncer is introduced (§6.5).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from starlette.requests import Request

# Deterministic naming convention so Alembic never emits database-generated
# constraint names — otherwise Phase 1's migrations become environment-dependent.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_db_engine(settings: Any) -> AsyncEngine:
    """Create an async SQLAlchemy engine from settings.

    Construction validates the URL shape but does NOT perform a connection.
    pool_pre_ping discards dead connections lazily on first use.
    """
    return create_async_engine(
        str(settings.database_url),
        pool_size=settings.database_pool_size,
        max_overflow=5,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=False,
        # Phase 1 note: behind PgBouncer in transaction mode this needs
        # connect_args={"statement_cache_size": 0} (§6.5).
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async sessionmaker with expire_on_commit=False.

    MANDATED by Research §0. With the default True, attribute access after
    commit triggers a lazy refresh — which raises MissingGreenlet in async
    code and silently breaks response serialisation. Non-negotiable.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Request-scoped session. Commits on success, rolls back on any exception."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def with_ef_search(session: AsyncSession, ef_search: int) -> None:
    """Tune HNSW recall for the current transaction only (Research §A0a).

    Uses SET LOCAL so the setting is scoped to the current transaction and
    automatically reverts at the end of it. This is the correct pattern for
    PgBouncer transaction-mode pooling (§6.5).
    """
    await session.execute(text("SET LOCAL hnsw.ef_search = :v"), {"v": ef_search})
