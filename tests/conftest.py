from collections.abc import AsyncGenerator

import pytest
from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper
from fsspec.implementations.local import LocalFileSystem
from playwright.async_api import async_playwright
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

import pravda.db as pravda_db
import pravda.storage as storage
from pravda.db import Base, SnapshotRecord

engine = pravda_db.engine


@pytest.fixture(scope="session")
async def db_schema():
    """Build a clean schema for the test session."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture()
async def db_session(db_schema) -> AsyncGenerator[AsyncSession, None]:
    """Provide an isolated session for dependency-injected database access."""
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest.fixture()
async def clean_snapshots(db_schema):
    """Remove rows committed through Pravda's own session factory."""
    yield
    async with pravda_db.async_session() as session:
        await session.execute(delete(SnapshotRecord))
        await session.commit()


@pytest.fixture(scope="session")
async def browser():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect("ws://localhost:3000")
    yield browser
    await browser.close()
    await playwright.stop()


@pytest.fixture()
def storage_tmp(tmp_path, monkeypatch):
    """Point artifact storage at an isolated local directory."""
    monkeypatch.setattr(storage, "fs", AsyncFileSystemWrapper(LocalFileSystem()))
    monkeypatch.setattr(storage, "_base_path", str(tmp_path))
    return tmp_path
