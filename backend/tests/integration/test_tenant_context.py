# SPDX-License-Identifier: FSL-1.1-ALv2
"""Transaction-scoped tenancy against a real database (§4.3, §6.7, §7.12, D-35).

The assertion that matters is not "the variable is set". It is that the variable is
**absent in the next transaction on the same pooled connection**. That is the only
observation which distinguishes `SET LOCAL` from `SET`, and getting it wrong is a
cross-tenant data leak that no single-request test can see: with `SET`, the value
persists for the life of the connection, so the next request to borrow it inherits the
previous tenant's id.

The pool is deliberately pinned to ONE connection, so "the same pooled connection" is
a fact rather than a hope. With a larger pool the second transaction might land on a
fresh connection and the test would pass for the wrong reason — which would make it
worse than no test.

Real Postgres, gated by `require_capability("postgres")`: it fails rather than skips
when `FORGEOPS_REQUIRE_INTEGRATION=1` (D-26).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from src.core.db import apply_tenant_context, create_sessionmaker
from src.core.tenancy import current_tenant_id, tenant_id_var

from .wiring import wires

pytestmark = pytest.mark.mandatory

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"

#: The setting the application reads. `current_setting(name, true)` returns NULL for a
#: never-set custom GUC rather than raising, which is what makes "absent" observable.
GUC = "app.tenant_id"


@pytest.fixture()
async def single_connection_engine(database_url: str) -> AsyncEngine:
    """An engine with exactly one pooled connection.

    `pool_size=1, max_overflow=0` is the whole point: it guarantees the second
    transaction reuses the first transaction's connection, so leakage is detectable.
    """
    engine = create_async_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def _read_guc(session) -> str | None:  # noqa: ANN001 - AsyncSession, imported lazily
    result = await session.execute(text("SELECT current_setting(:name, true)"), {"name": GUC})
    return result.scalar()


@wires("engine", "sessionmaker")
class TestTransactionScopedTenancy:
    async def test_the_tenant_is_visible_inside_its_own_transaction(
        self, single_connection_engine: AsyncEngine
    ) -> None:
        factory = create_sessionmaker(single_connection_engine)
        token = tenant_id_var.set(TENANT_A)
        try:
            async with factory() as session:
                await apply_tenant_context(session)
                assert await _read_guc(session) == TENANT_A
                await session.commit()
        finally:
            tenant_id_var.reset(token)

    async def test_it_is_absent_in_the_next_transaction_on_the_same_connection(
        self, single_connection_engine: AsyncEngine
    ) -> None:
        """The clause that proves SET LOCAL rather than SET.

        With `SET`, the second read below returns TENANT_A and this test fails — which
        is exactly the cross-tenant leak the design forbids.
        """
        factory = create_sessionmaker(single_connection_engine)

        token = tenant_id_var.set(TENANT_A)
        try:
            async with factory() as session:
                await apply_tenant_context(session)
                assert await _read_guc(session) == TENANT_A
                await session.commit()
        finally:
            tenant_id_var.reset(token)

        # Same engine, same single pooled connection, no tenant in context.
        async with factory() as session:
            leaked = await _read_guc(session)
            await session.commit()

        assert leaked in (None, ""), (
            f"tenant {leaked!r} leaked into the next transaction on the same pooled "
            "connection. That is `SET` behaviour, not `SET LOCAL` (design §6.7, D-35)."
        )

    async def test_a_second_tenant_does_not_see_the_first(self, single_connection_engine: AsyncEngine) -> None:
        """The leak in its most direct form: two tenants, one connection."""
        factory = create_sessionmaker(single_connection_engine)

        for tenant in (TENANT_A, TENANT_B):
            token = tenant_id_var.set(tenant)
            try:
                async with factory() as session:
                    await apply_tenant_context(session)
                    assert await _read_guc(session) == tenant
                    await session.commit()
            finally:
                tenant_id_var.reset(token)

    async def test_a_rollback_also_reverts_the_setting(self, single_connection_engine: AsyncEngine) -> None:
        """`SET LOCAL` reverts at ROLLBACK too, and the failure path is the one that
        matters: an aborted request must not hand its tenant to the next one."""
        factory = create_sessionmaker(single_connection_engine)

        token = tenant_id_var.set(TENANT_B)
        try:
            async with factory() as session:
                await apply_tenant_context(session)
                assert await _read_guc(session) == TENANT_B
                await session.rollback()
        finally:
            tenant_id_var.reset(token)

        async with factory() as session:
            assert await _read_guc(session) in (None, "")
            await session.commit()

    async def test_no_tenant_in_context_issues_no_statement(self, single_connection_engine: AsyncEngine) -> None:
        """A public route has no tenant, and must not get a placeholder.

        Writing something like `'unknown'` would be worse than nothing: a Phase 2 RLS
        policy could match it, and rows would become visible to a scope that does not
        exist.
        """
        factory = create_sessionmaker(single_connection_engine)
        assert current_tenant_id() is None
        async with factory() as session:
            assert await apply_tenant_context(session) is None
            assert await _read_guc(session) in (None, "")
            await session.commit()


class TestDeferredByD35:
    """D-35 keeps RLS and NOT NULL for Phase 2. Assert we did not sneak them in."""

    async def test_no_row_level_security_policy_exists(self, single_connection_engine: AsyncEngine) -> None:
        async with single_connection_engine.connect() as conn:
            policies = (await conn.execute(text("SELECT count(*) FROM pg_policies"))).scalar()
        assert policies == 0, (
            f"{policies} RLS policies exist; D-35 defers RLS to Phase 2, and enabling it "
            "early is an irreversible commitment made before there is a multi-tenant "
            "product to shape it"
        )

    async def test_tenant_id_columns_remain_nullable(self, single_connection_engine: AsyncEngine) -> None:
        async with single_connection_engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT table_name, is_nullable FROM information_schema.columns "
                        "WHERE column_name = 'tenant_id' AND table_schema = 'public'"
                    )
                )
            ).all()
        not_null = [name for name, nullable in rows if nullable == "NO"]
        assert not not_null, f"tenant_id is NOT NULL on {not_null}; D-35 defers that to Phase 2"


class TestPoolerMode:
    """`DATABASE_POOLER_MODE=transaction` must disable the statement cache (§7.12)."""

    def test_transaction_mode_disables_both_caches(self) -> None:
        from src.core.db import pooler_connect_args

        args = pooler_connect_args("transaction")
        assert args["statement_cache_size"] == 0
        assert args["prepared_statement_cache_size"] == 0

    def test_session_mode_adds_nothing(self) -> None:
        """The default must not pay the cost of a pooler that is not there."""
        from src.core.db import pooler_connect_args

        assert pooler_connect_args("session") == {}

    def test_the_engine_carries_the_mode_through(self) -> None:
        from src.core.config import Settings
        from src.core.db import create_db_engine

        engine = create_db_engine(
            Settings(
                database_url="postgresql+asyncpg://u:p@localhost/db",
                redis_url="redis://localhost:6379/0",
                database_pooler_mode="transaction",
            )
        )
        assert engine.url.drivername == "postgresql+asyncpg"
