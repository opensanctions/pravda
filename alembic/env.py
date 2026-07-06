"""Alembic migration environment.

Builds a one-shot async engine (asyncpg) from ``DATABASE_URL`` for each run,
runs the migrations, and disposes it on the same event loop. ``Base.metadata``
is imported from the app so autogenerate sees the current models. Online only;
there is no offline SQL generation.
"""

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from pravda.db import Base

# this is the Alembic Config object, which provides access to .ini values.
config = context.config

# Interpret the .ini for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Model metadata used for autogenerate support.
target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(
        os.environ["DATABASE_URL"], poolclass=pool.NullPool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    raise ValueError("Offline mode is not supported. Use online mode only.")
else:
    run_migrations_online()
