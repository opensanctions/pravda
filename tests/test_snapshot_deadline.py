"""Wall-clock bounds for ``Pravda.snapshot``."""

import asyncio
import os
import time
from dataclasses import replace

import pytest
from playwright.async_api import Browser, BrowserContext
from sqlalchemy.ext.asyncio import AsyncSession

from pravda import Pravda, PravdaConfig, Timeouts
from tests.helpers import fulfill_html


async def _drive_example(page, url):
    await page.route(url, fulfill_html)
    await page.goto(url, wait_until="load")


def _pravda(sessionmaker, tmp_path, **budgets: float) -> Pravda:
    """A client with the given budget fields shrunk from the defaults."""
    config = PravdaConfig(
        browser_ws_url=os.environ["PRAVDA_TEST_BROWSER_WS_URL"],
        storage_base_path=str(tmp_path),
        timeouts=replace(Timeouts(), **budgets),
    )
    return Pravda(config, sessionmaker)


@pytest.mark.asyncio
async def test_snapshot_drive_timeout_persists_failed_attempt(sessionmaker, tmp_path):
    pravda = _pravda(sessionmaker, tmp_path, drive_milliseconds=200)

    async def drive(page, url):
        await asyncio.sleep(5)

    snapshot = await pravda.snapshot("https://example.com", drive=drive)

    assert "drive" in snapshot.error.lower()
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_snapshot_total_timeout_persists_failed_attempt(
    sessionmaker, tmp_path, monkeypatch
):
    pravda = _pravda(sessionmaker, tmp_path, snapshot_milliseconds=200)

    async def slow_new_context(self, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(Browser, "new_context", slow_new_context)
    snapshot = await pravda.snapshot("https://example.com")

    assert "wall-clock" in snapshot.error
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_context_close_timeout_discards_evidence(
    sessionmaker, tmp_path, monkeypatch
):
    pravda = _pravda(sessionmaker, tmp_path, context_close_milliseconds=10)

    async def slow_close(self, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(BrowserContext, "close", slow_close)
    snapshot = await pravda.snapshot("https://example.com", drive=_drive_example)

    assert "context close" in snapshot.error
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.screenshot is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_snapshot_persistence_timeout_propagates(
    sessionmaker, tmp_path, monkeypatch
):
    pravda = _pravda(sessionmaker, tmp_path, persist_milliseconds=200)

    async def hanging_commit(self):
        await asyncio.sleep(5)

    monkeypatch.setattr(AsyncSession, "commit", hanging_commit)

    with pytest.raises(asyncio.TimeoutError):
        await pravda.snapshot("https://example.com", drive=_drive_example)

    assert await pravda.snapshots("https://example.com") == []


@pytest.mark.asyncio
async def test_snapshot_cleanup_is_bounded_and_keeps_outcome(
    sessionmaker, tmp_path, monkeypatch
):
    pravda = _pravda(sessionmaker, tmp_path, cleanup_milliseconds=200)

    async def slow_close(self, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(Browser, "close", slow_close)

    start = time.monotonic()
    snapshot = await pravda.snapshot("https://example.com", drive=_drive_example)

    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.http_archive is not None
    assert time.monotonic() - start < 4


@pytest.mark.asyncio
async def test_snapshot_cancellation_propagates(pravda: Pravda):
    started = asyncio.Event()
    block = asyncio.Event()

    async def drive(page, url):
        started.set()
        await block.wait()

    task = asyncio.create_task(pravda.snapshot("https://example.com", drive=drive))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert await pravda.snapshots("https://example.com") == []
