"""Wall-clock bounds for ``Pravda.snapshot``."""

import asyncio
import time
from pathlib import Path

import pytest
from playwright.async_api import Browser, BrowserContext
from sqlalchemy.ext.asyncio import AsyncSession

import pravda.capture as capture_module
import pravda.pravda as pravda_module
from pravda import Pravda

FIXTURES = Path(__file__).parent / "fixtures"


def _fulfill_html(route):
    return route.fulfill(
        body=(FIXTURES / "example.html").read_text(),
        headers={"content-type": "text/html"},
    )


async def _drive_example(page, url):
    await page.route(url, _fulfill_html)
    await page.goto(url, wait_until="load")


@pytest.mark.asyncio
async def test_snapshot_drive_timeout_persists_failed_attempt(
    pravda: Pravda, monkeypatch
):
    monkeypatch.setattr(capture_module, "DRIVE_TIMEOUT_S", 0.2)

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
    pravda: Pravda, monkeypatch
):
    monkeypatch.setattr(pravda_module, "SNAPSHOT_TIMEOUT_S", 0.2)

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
async def test_context_close_timeout_discards_evidence(pravda: Pravda, monkeypatch):
    monkeypatch.setattr(pravda_module, "CONTEXT_CLOSE_TIMEOUT_S", 0.01)

    async def slow_close(self, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(BrowserContext, "close", slow_close)
    snapshot = await pravda.snapshot("https://example.com", drive=_drive_example)

    assert "context close" in snapshot.error
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.plaintext is None
    assert snapshot.rendered_html is None
    assert snapshot.screenshot is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_storage_timeout_is_not_mistaken_for_snapshot_timeout(
    pravda: Pravda, monkeypatch
):
    monkeypatch.setattr(capture_module, "STORAGE_WRITE_TIMEOUT_S", 0.01)

    async def slow_pipe_file(path, value, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(pravda._storage.fs, "_pipe_file", slow_pipe_file)

    with pytest.raises(asyncio.TimeoutError):
        await pravda.snapshot("https://example.com", drive=_drive_example)

    assert await pravda.snapshots("https://example.com") == []


@pytest.mark.asyncio
async def test_snapshot_persistence_timeout_propagates(pravda: Pravda, monkeypatch):
    monkeypatch.setattr(pravda_module, "PERSIST_TIMEOUT_S", 0.2)

    async def hanging_commit(self):
        await asyncio.sleep(5)

    monkeypatch.setattr(AsyncSession, "commit", hanging_commit)

    with pytest.raises(asyncio.TimeoutError):
        await pravda.snapshot("https://example.com", drive=_drive_example)

    assert await pravda.snapshots("https://example.com") == []


@pytest.mark.asyncio
async def test_snapshot_cleanup_is_bounded_and_keeps_outcome(
    pravda: Pravda, monkeypatch
):
    async def slow_close(self, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(pravda_module, "CLEANUP_TIMEOUT_S", 0.2)
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
