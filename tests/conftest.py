from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from pravda.constants import TEST_DATABASE_URL
from pravda.db import Base

engine = create_async_engine(TEST_DATABASE_URL)


@pytest.fixture(scope="session")
async def db_engine():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture()
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    connection = await db_engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint")

    yield session

    await session.close()
    await transaction.rollback()
    await connection.close()


@pytest.fixture()
def storage_dir(tmp_path: Path, monkeypatch):
    """Redirect blob storage to a temp directory."""
    monkeypatch.setattr("pravda.constants.STORAGE_BASE_PATH", str(tmp_path / "blobs"))
    return tmp_path / "blobs"
