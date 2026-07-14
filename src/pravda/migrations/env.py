"""Alembic migration environment.

Shared by two callers:

* The developer command (``alembic.ini`` at the repository root), which reads
  the database URL from ``DATABASE_URL``.
* The public :func:`pravda.migrate` API, which sets the URL on the Alembic
  config explicitly and so needs no environment variable.

In both cases a one-shot async engine (asyncpg) is built from the resolved
URL, the migrations run, and the engine is disposed on the same event loop.
``Base.metadata`` is imported from the app so autogenerate sees the current
models. Online only; there is no offline SQL generation.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from pravda.db import Base

# this is the Alembic Config object, which provides access to .ini values.
config = context.config

# Interpret the .ini for Python logging. Skipped when run programmatically
# (no config file); Alembic's own logging then applies.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Model metadata used for autogenerate support.
target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # The database URL is supplied either on the Alembic config (by the public
    # pravda.migrate API) or, for the developer command, via DATABASE_URL.
    database_url = config.get_main_option("sqlalchemy.url") or os.environ.get(
        "DATABASE_URL"
    )
    if not database_url:
        raise RuntimeError(
            "No database URL configured: pass one to pravda.migrate() or set "
            "DATABASE_URL for the alembic command."
        )
    connectable = create_async_engine(database_url, poolclass=pool.NullPool)
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    raise ValueError("Offline mode is not supported. Use online mode only.")
else:
    run_migrations_online()
