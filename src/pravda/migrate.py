"""Upgrade a database with Pravda's packaged Alembic migrations."""

import asyncio
from importlib.resources import files

from alembic import command
from alembic.config import Config

__all__ = ["migrate"]


def _config(database_url: str) -> Config:
    """Build an Alembic config for the packaged migrations."""
    config = Config()
    config.set_main_option("script_location", str(files("pravda") / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


async def migrate(database_url: str) -> None:
    """Upgrade *database_url* to the packaged Alembic head."""
    await asyncio.to_thread(command.upgrade, _config(database_url), "head")
