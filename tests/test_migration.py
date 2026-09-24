"""Tests for the public migration API and packaged migration resources."""

import inspect
import uuid
from datetime import datetime
from importlib.resources import files

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

import pravda
from pravda.db import SnapshotRecord

# Literal (not reflected from Base.metadata) so the test asserts what the
# migrations produced, not what the models declare.
EXPECTED_COLUMNS = {
    "id": "UUID",
    "url": "TEXT",
    "final_url": "TEXT",
    "captured_at": "DATETIME",
    "http_status": "INTEGER",
    "error": "TEXT",
    "plaintext": "TEXT",
    "rendered_html": "TEXT",
    "screenshot": "TEXT",
    "http_archive": "JSON",
}


@pytest.fixture()
def database_url() -> str:
    """An isolated in-memory SQLite URL for the migration API to target."""
    return (
        f"sqlite+aiosqlite:///file:{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true"
    )


@pytest.fixture()
async def database(database_url):
    """Hold the in-memory database open while ``migrate`` opens its own engine."""
    engine = create_async_engine(database_url, poolclass=AsyncAdaptedQueuePool)
    keepalive = await engine.connect()
    yield engine
    await keepalive.close()
    await engine.dispose()


async def _snapshot_columns(engine) -> dict[str, str]:
    async with engine.connect() as connection:
        rows = await connection.execute(text("PRAGMA table_info(snapshot)"))
        return {row[1]: row[2] for row in rows.all()}


async def _alembic_version(engine) -> str | None:
    async with engine.connect() as connection:
        return (
            await connection.execute(
                text("SELECT version_num FROM pravda_alembic_version")
            )
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
async def test_migrate_creates_expected_schema(database_url, database):
    """Migrating an empty database creates the snapshot table and stamps head."""
    await pravda.migrate(database_url)

    assert await _snapshot_columns(database) == EXPECTED_COLUMNS
    assert await _alembic_version(database) is not None


@pytest.mark.asyncio
async def test_migrate_to_head_is_idempotent(database_url, database):
    """Migrating an already-at-head database is a safe no-op."""
    await pravda.migrate(database_url)
    first_version = await _alembic_version(database)

    await pravda.migrate(database_url)
    assert await _alembic_version(database) == first_version
    assert await _snapshot_columns(database) == EXPECTED_COLUMNS


@pytest.mark.asyncio
async def test_migrate_creates_working_schema(database_url, database):
    """The migrated schema round-trips models with UUID, JSON, and ordering."""
    await pravda.migrate(database_url)

    sessionmaker = async_sessionmaker(database, expire_on_commit=False)
    early = SnapshotRecord(
        url="https://example.com",
        captured_at=datetime(2024, 1, 1),
    )
    late = SnapshotRecord(
        url="https://example.com",
        captured_at=datetime(2024, 1, 2),
        http_archive={"log": {"entries": []}},
    )
    defaulted = SnapshotRecord(url="https://example.com")
    async with sessionmaker() as session:
        session.add_all([early, late, defaulted])
        await session.commit()
        assert isinstance(defaulted.id, uuid.UUID)

    async with sessionmaker() as session:
        result = await session.execute(
            select(SnapshotRecord).order_by(SnapshotRecord.captured_at.desc())
        )
        rows = result.scalars().all()

    assert [row.id for row in rows] == [defaulted.id, late.id, early.id]
    assert rows[0].captured_at.tzinfo is None
    assert rows[1].captured_at == late.captured_at
    assert rows[1].http_archive == {"log": {"entries": []}}


@pytest.mark.asyncio
async def test_migrate_runs_inside_running_event_loop(database_url, database):
    """Migration works from within a running event loop (no nested asyncio.run)."""
    await pravda.migrate(database_url)
    assert await _alembic_version(database) is not None


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
