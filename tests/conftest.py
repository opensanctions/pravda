import os
from collections.abc import AsyncGenerator

import pytest
from playwright.async_api import async_playwright
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from pravda.db import Base

engine = create_async_engine(os.environ["TEST_DATABASE_URL"])


@pytest.fixture(scope="session")
async def db_engine():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(scope="session")
async def browser():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect("ws://localhost:3000")
    yield browser
    await browser.close()
    await pw.stop()


@pytest.fixture()
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    connection = await db_engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint")

    yield session

    await session.close()
    await transaction.rollback()
    await connection.close()
