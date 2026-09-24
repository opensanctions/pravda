"""Shared pytest fixtures for the test browser and database."""

import os
import uuid

import pytest
from playwright.async_api import async_playwright
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from pravda import Pravda, PravdaConfig
from pravda.db import Base
from pravda.storage import Storage

BROWSER_WS_URL = os.environ.get("PRAVDA_TEST_BROWSER_WS_URL", "ws://localhost:3000")


@pytest.fixture()
def pravda_config(tmp_path) -> PravdaConfig:
    """Configuration for a client with an isolated artifact store."""
    return PravdaConfig(
        browser_ws_url=BROWSER_WS_URL,
        storage_base_path=str(tmp_path),
    )


@pytest.fixture()
async def sessionmaker():
    """A fresh in-memory SQLite database shared by one test's connections.

    A named shared-cache database gives each session its own connection and
    transaction state, and is discarded when the test's engine is disposed.
    """
    url = (
        f"sqlite+aiosqlite:///file:{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true"
    )
    engine = create_async_engine(url, poolclass=AsyncAdaptedQueuePool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture()
async def pravda(sessionmaker, pravda_config: PravdaConfig):
    """A configured client sharing the test's in-memory database."""
    return Pravda(pravda_config, sessionmaker)


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
