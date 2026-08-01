# SPDX-License-Identifier: FSL-1.1-ALv2
"""Alembic async environment (design.md §6.3–§6.4).

- Async migrations via asyncpg.
- Uses SQLModel.metadata with compare_type=True.
- render_item teaches autogenerate the pgvector Vector type so it does not
  produce spurious drop/recreate diffs on every revision.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from logging.config import fileConfig

from alembic import context
from pgvector.sqlalchemy import Vector
from sqlalchemy import pool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

# Ensure the backend src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import all models so SQLModel.metadata is populated. A model that is not imported
# here is invisible to `--autogenerate`, which then proposes dropping its table:
# `test_alembic_autogenerate_clean.py` (task 5.9) is what turns that into a failure
# rather than a surprise in a later diff.
from src.analysis.models import (  # noqa: F401, E402
    AnalysisReport,
    Embedding,
    EmbeddingLocal,
    FileContent,
    FileDependency,
    FileTreeEntry,
)
from src.audit.models import AuditEvent  # noqa: F401, E402
from src.auth.device_models import AgentDevice  # noqa: F401, E402
from src.auth.models import Session, User  # noqa: F401, E402
from src.generation.models import GenerationRun  # noqa: F401, E402
from src.governance.models import (  # noqa: F401, E402
    Approval,
    ChangeItem,
    ChangeSet,
    RollbackHandle,
    Validation,
)
from src.policies.models import Policy, PolicyBundle, PolicyEvaluation  # noqa: F401, E402
from src.projects.models import Project, ProjectTag  # noqa: F401, E402
from src.secrets.models import Secret  # noqa: F401, E402

# Alembic Config object
config = context.config

# Set up logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Which URL Alembic connects with — and it is NOT `DATABASE_URL` when a migrator URL
# is configured (design §6.4, §13.1).
#
# §6.4: "`DATABASE_URL` uses `forgeops_app`, which cannot UPDATE or DELETE audit rows;
# `ALEMBIC_DATABASE_URL` uses `forgeops_migrator`, which owns the schema. A single-role
# deployment silently defeats mechanism 3." This file previously read `DATABASE_URL`
# only, so `alembic upgrade head` ran as the APPLICATION role: the app would then own
# `audit_events` and could drop its own append-only triggers, which is exactly the
# arrangement §6.4 says must not happen. `ALEMBIC_DATABASE_URL` was registered in
# `core/config.py` and shipped in `.env.example`, and nothing read it.
#
# `ALEMBIC_DATABASE_URL` wins when set; `DATABASE_URL` remains the fallback so a
# single-role development database keeps working. The choice is announced, because a
# migration running as the wrong role is invisible otherwise.
database_url = os.environ.get("ALEMBIC_DATABASE_URL") or os.environ.get("DATABASE_URL")
_source = "alembic.ini sqlalchemy.url"
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)
    _source = "ALEMBIC_DATABASE_URL" if os.environ.get("ALEMBIC_DATABASE_URL") else "DATABASE_URL"
    # Credentials are never printed: only which variable was chosen.
    print(f"alembic: connecting with {_source}", file=sys.stderr)

target_metadata = SQLModel.metadata


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Teach autogenerate to emit pgvector columns correctly.

    Without this, autogenerate does not know the Vector type and produces a
    spurious drop/recreate on every revision (design.md §6.3).
    """
    if type_ == "type" and isinstance(obj, Vector):
        autogen_context.imports.add("from pgvector.sqlalchemy import Vector")  # type: ignore[attr-defined]
        return f"Vector({obj.dim})"
    return False


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (SQL generation only)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
        compare_type=True,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    """Configure and run migrations with a live connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_item=render_item,
        compare_type=True,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations asynchronously using asyncpg."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    except socket.gaierror as exc:
        # D-75. Name the variable, the host and the remedy, because the bare failure names
        # none of them.
        #
        # `make init-env` copies `.env.example` to `.env`, and `.env.example` is
        # COMPOSE-targeted: its DSNs use the service names `postgres` and `redis`, which
        # resolve only on the Compose network. `.env` also holds the development CA key
        # (`make init-ca`), so a host-side developer has a real reason to load it - and
        # loading it wholesale puts `ALEMBIC_DATABASE_URL=...@postgres:5432` into the OS
        # environment. `os.environ` outranks anything a test fixture configures, this file
        # prefers `ALEMBIC_DATABASE_URL` over `DATABASE_URL` by design (§6.4, and that
        # preference is correct - it must not be relaxed), and so every DB-backed test
        # errors at setup with `socket.gaierror: [Errno 11001] getaddrinfo failed` from
        # `schema_at_head`'s `alembic downgrade base`. A Linux developer hits the identical
        # thing; nothing here is Windows-specific. Recorded as finding 61.
        host = make_url(config.get_main_option("sqlalchemy.url") or "").host
        raise RuntimeError(
            f"alembic: cannot resolve database host {host!r}, taken from {_source}. "
            f"If {host!r} is a Compose service name you are running outside Compose: "
            f"`.env.example` (and therefore `.env`) is Compose-targeted, and loading `.env` "
            f"wholesale for its CA key also imports its Compose DSNs. Set "
            f"ALEMBIC_DATABASE_URL to a host-reachable DSN, or load `.env` selectively - "
            f"scripts/local-env.ps1 does the latter. See docs/development.md, "
            f"'The .env and ALEMBIC_DATABASE_URL trap'."
        ) from exc
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with a live database."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
