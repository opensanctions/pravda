"""Public migration API: upgrade a database to the packaged schema head.

Alembic owns the Pravda schema. The packaged migration scripts live in
:mod:`pravda.migrations` and ship inside the distribution; this module runs
them against a caller-supplied database URL. Use it from application startup
to bring a database up to the current Pravda schema::

    import pravda

    await pravda.migrate(database_url)

The URL is passed explicitly — the installed-library API never reads
``DATABASE_URL`` from the environment (the developer ``alembic`` command may).
Migration and database failures propagate. The one-shot engine the runner
creates is disposed before it returns.
"""

import asyncio
from importlib.resources import files

from alembic import command
from alembic.config import Config

__all__ = ["migrate"]


def _config(database_url: str) -> Config:
    """Build an Alembic config pointing at the packaged migration scripts.

    ``script_location`` resolves to the installed ``pravda/migrations``
    directory via :func:`importlib.resources.files`, so resource lookup works
    from an installed wheel and does not depend on the current working
    directory or a source checkout.
    """
    config = Config()
    config.set_main_option("script_location", str(files("pravda") / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


async def migrate(database_url: str) -> None:
    """Upgrade *database_url* to the packaged Alembic head.

    Runs the packaged revisions through Alembic (not ``create_all``). The URL
    is passed explicitly and must be an async SQLAlchemy Postgres URL (e.g.
    ``postgresql+asyncpg://user:pass@host/db``). Repeated calls are safe: a
    database already at head is a no-op.

    Alembic's command interface is synchronous and its environment drives
    migrations with ``asyncio.run``; running the command in a worker thread
    via :func:`asyncio.to_thread` keeps this function safe to call from inside
    an already-running event loop (no nested ``asyncio.run``). The one-shot
    engine the migration environment creates is disposed before the call
    returns. Database and migration errors propagate to the caller.
    """
    await asyncio.to_thread(command.upgrade, _config(database_url), "head")
