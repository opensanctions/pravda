"""Alembic migration environment.

Reuses the async engine from :mod:`pravda.db` (asyncpg) and the declarative
``Base.metadata`` so autogenerate sees the current models. Runs online only;
there is no offline SQL generation.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy.engine import Connection

from alembic import context
from pravda.db import Base, engine

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
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    raise ValueError("Offline mode is not supported. Use online mode only.")
else:
    run_migrations_online()
