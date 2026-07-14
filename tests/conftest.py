"""Shared pytest fixtures for the Compose browser and test database."""

import pytest
from playwright.async_api import async_playwright
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pravda import Pravda, PravdaConfig
from pravda.db import Base, SnapshotRecord
from pravda.storage import Storage

DATABASE_URL = "postgresql+asyncpg://pravda:pravda@localhost:5432/pravda"
BROWSER_WS_URL = "ws://localhost:3000"


@pytest.fixture()
def pravda_config(tmp_path) -> PravdaConfig:
    """Configuration for a client with an isolated artifact store."""
    return PravdaConfig(
        database_url=DATABASE_URL,
        browser_ws_url=BROWSER_WS_URL,
        storage_base_path=str(tmp_path),
    )


@pytest.fixture(scope="session")
async def database_engine():
    """Create the test schema and own its engine for the test session."""
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture()
async def database(database_engine):
    """Provide database access and clear committed snapshots around each test."""
    sessionmaker = async_sessionmaker(database_engine, expire_on_commit=False)
    async with sessionmaker.begin() as session:
        await session.execute(delete(SnapshotRecord))
    yield sessionmaker
    async with sessionmaker.begin() as session:
        await session.execute(delete(SnapshotRecord))


@pytest.fixture()
async def pravda(database, pravda_config: PravdaConfig):
    """A configured client with per-test database and storage isolation."""
    async with Pravda(pravda_config) as instance:
        yield instance


@pytest.fixture()
def storage(tmp_path):
    """An isolated content-addressed store pointing at a temporary directory."""
    return Storage.from_url(str(tmp_path))


@pytest.fixture(scope="session")
async def browser():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect(BROWSER_WS_URL)
    yield browser
    await browser.close()
    await playwright.stop()
