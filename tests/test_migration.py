"""Tests for the public migration API and packaged migration resources."""

import inspect
from importlib.resources import files

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import pravda
from pravda.db import Base

DATABASE_URL = "postgresql+asyncpg://pravda:pravda@localhost:5432/pravda"

# Literal (not reflected from Base.metadata) so the test asserts what
# migrations produced, not what the models declare.
EXPECTED_COLUMNS = {
    "id": "uuid",
    "url": "text",
    "final_url": "text",
    "captured_at": "timestamp with time zone",
    "http_status": "integer",
    "error": "text",
    "plaintext": "text",
    "rendered_html": "text",
    "screenshot": "text",
    "http_archive": "jsonb",
}


@pytest.fixture()
async def empty_database():
    """Drop the public schema for a migration test and restore ``create_all`` after."""
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


async def _snapshot_columns(engine) -> dict[str, str]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'snapshot' "
                "ORDER BY ordinal_position"
            )
        )
        return {name: dtype for name, dtype in result.all()}


async def _alembic_version(engine) -> str | None:
    async with engine.connect() as conn:
        return (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar()


@pytest.mark.asyncio
async def test_migrate_is_async_and_exported():
    """``migrate`` is an awaitable public export with an explicit URL param."""
    assert "migrate" in pravda.__all__
    assert callable(pravda.migrate)
    assert inspect.iscoroutinefunction(pravda.migrate)
    params = list(inspect.signature(pravda.migrate).parameters)
    assert params == ["database_url"]


@pytest.mark.asyncio
async def test_migrate_creates_expected_schema(empty_database):
    """Migrating an empty database creates the snapshot table and stamps head."""
    await pravda.migrate(DATABASE_URL)

    assert await _snapshot_columns(empty_database) == EXPECTED_COLUMNS
    assert await _alembic_version(empty_database) is not None


@pytest.mark.asyncio
async def test_migrate_to_head_is_idempotent(empty_database):
    """Migrating an already-at-head database is a safe no-op."""
    await pravda.migrate(DATABASE_URL)
    first_version = await _alembic_version(empty_database)

    await pravda.migrate(DATABASE_URL)
    assert await _alembic_version(empty_database) == first_version
    assert await _snapshot_columns(empty_database) == EXPECTED_COLUMNS


@pytest.mark.asyncio
async def test_migrate_runs_inside_running_event_loop(empty_database):
    """Migration works from within a running event loop (no nested asyncio.run)."""
    await pravda.migrate(DATABASE_URL)
    assert await _alembic_version(empty_database) is not None


@pytest.mark.asyncio
async def test_migrate_does_not_require_database_url_env(monkeypatch):
    """The public API needs no DATABASE_URL in the environment."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    migrations = files("pravda") / "migrations"
    assert (migrations / "env.py").is_file()


def test_packaged_migration_resources_are_present():
    """The migration environment and every revision ship inside the package."""
    migrations = files("pravda") / "migrations"
    assert (migrations / "env.py").is_file()
    assert (migrations / "script.py.mako").is_file()

    versions = migrations / "versions"
    revision_files = [
        child.name for child in versions.iterdir() if child.name.endswith(".py")
    ]
    assert revision_files, "no packaged migration revisions found"
