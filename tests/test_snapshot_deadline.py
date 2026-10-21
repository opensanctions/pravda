"""Wall-clock budgets for ``Pravda.capture``."""

import asyncio
import os
from dataclasses import replace

import pytest
from playwright.async_api import BrowserContext, BrowserType

import pravda.pravda as pravda_module
from pravda import InfrastructureError, Pravda, Timeouts
from tests.helpers import fulfill_html


async def _drive_example(page, url):
    await page.route(url, fulfill_html)
    await page.goto(url, wait_until="load")


def _pravda(storage, **budgets: float) -> Pravda:
    """A client with the given budget fields shrunk from the defaults."""
    return Pravda(
        os.environ["PRAVDA_TEST_BROWSER_WS_URL"],
        storage,
        timeouts=replace(Timeouts(), dom_settle_quiet=0.05, **budgets),
    )


@pytest.mark.asyncio
async def test_capture_drive_timeout_returns_failed_snapshot(storage):
    pravda = _pravda(storage, drive=0.2)

    async def drive(page, url):
        await asyncio.sleep(5)

    snapshot = await pravda.capture("https://example.com", drive=drive)

    assert "drive" in snapshot.error.lower()
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_capture_wall_clock_timeout_returns_failed_snapshot(storage):
    """The wall-clock breaker expires while no inner budget has."""
    pravda = _pravda(storage, snapshot=0.2)

    async def drive(page, url):
        await asyncio.sleep(5)

    snapshot = await pravda.capture("https://example.com", drive=drive)

    assert "wall-clock" in snapshot.error
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_setup_timeout_propagates(storage, monkeypatch):
    monkeypatch.setattr(pravda_module, "_SETUP_BUDGET", 0.2)
    pravda = _pravda(storage)

    async def slow_connect(self, url, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(BrowserType, "connect", slow_connect)

    with pytest.raises(InfrastructureError, match="setup"):
        await pravda.capture("https://example.com", drive=_drive_example)


@pytest.mark.asyncio
async def test_context_close_timeout_returns_failed_snapshot(storage, monkeypatch):
    pravda = _pravda(storage, close=0.2)

    async def slow_close(self, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(BrowserContext, "close", slow_close)
    snapshot = await pravda.capture("https://example.com", drive=_drive_example)

    assert "context close" in snapshot.error
    assert snapshot.http_status is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_capture_cancellation_propagates(pravda: Pravda):
    started = asyncio.Event()
    block = asyncio.Event()

    async def drive(page, url):
        started.set()
        await block.wait()

    task = asyncio.create_task(pravda.capture("https://example.com", drive=drive))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
