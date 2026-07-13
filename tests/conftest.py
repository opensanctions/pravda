import pytest
from fsspec.implementations.asyn_wrapper import AsyncFileSystemWrapper
from fsspec.implementations.local import LocalFileSystem
from playwright.async_api import async_playwright

import pravda.db as pravda_db
import pravda.storage as storage
from pravda.db import Base

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


@pytest.fixture(autouse=True)
async def db_rollback(db_schema):
    """Isolate each test behind one rolled-back outer transaction.

    Pravda commits through its own session factory. Rebinding that factory to a
    single connection joined to an outer transaction (via savepoints) lets
    those commits land in the transaction; rolling it back undoes them all.
    """
    async with pravda_db.engine.connect() as connection:
        transaction = await connection.begin()
        pravda_db.async_session.configure(
            bind=connection, join_transaction_mode="create_savepoint"
        )
        try:
            yield
        finally:
            try:
                await transaction.rollback()
            finally:
                pravda_db.async_session.configure(
                    bind=pravda_db.engine,
                    join_transaction_mode="conditional_savepoint",
                )


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
