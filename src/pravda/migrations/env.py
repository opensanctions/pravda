"""Online Alembic environment for the CLI and public migration API."""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from pravda.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
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
