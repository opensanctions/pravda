"""Liveness and wall-clock bounds for ``Pravda.capture``."""

import asyncio
import os
from dataclasses import replace

import pytest
from playwright.async_api import BrowserContext, BrowserType

from pravda import InfrastructureError, Liveness, Pravda, PravdaConfig, Timeouts
from tests.helpers import fulfill_html


async def _drive_example(page, url):
    await page.route(url, fulfill_html)
    await page.goto(url, wait_until="load")


def _pravda(tmp_path, liveness=None, **budgets: float) -> Pravda:
    """A client with the given budget fields shrunk from the defaults."""
    config = PravdaConfig(
        browser_ws_url=os.environ["PRAVDA_TEST_BROWSER_WS_URL"],
        storage_base_path=str(tmp_path),
        timeouts=replace(Timeouts(), dom_settle_quiet=0.05, **budgets),
        liveness=liveness or Liveness(),
    )
    return Pravda(config)


@pytest.mark.asyncio
async def test_capture_drive_timeout_returns_failed_snapshot(tmp_path):
    pravda = _pravda(tmp_path, drive=0.2)

    async def drive(page, url):
        await asyncio.sleep(5)

    snapshot = await pravda.capture("https://example.com", drive=drive)

    assert "drive" in snapshot.error.lower()
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_capture_wall_clock_timeout_returns_failed_snapshot(tmp_path):
    """The wall-clock breaker expires while no inner budget has."""
    pravda = _pravda(tmp_path, snapshot=0.2)

    async def drive(page, url):
        await asyncio.sleep(5)

    snapshot = await pravda.capture("https://example.com", drive=drive)

    assert "wall-clock" in snapshot.error
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_setup_timeout_propagates(tmp_path, monkeypatch):
    pravda = _pravda(tmp_path, liveness=Liveness(setup=0.2))

    async def slow_connect(self, url, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(BrowserType, "connect", slow_connect)

    with pytest.raises(InfrastructureError, match="setup"):
        await pravda.capture("https://example.com", drive=_drive_example)


@pytest.mark.asyncio
async def test_context_close_timeout_raises(tmp_path, monkeypatch):
    pravda = _pravda(tmp_path, liveness=Liveness(close=0.2))

    async def slow_close(self, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(BrowserContext, "close", slow_close)
    with pytest.raises(InfrastructureError, match="context close"):
        await pravda.capture("https://example.com", drive=_drive_example)


def test_liveness_deadlines_must_be_positive():
    with pytest.raises(ValueError, match="setup"):
        Liveness(setup=0)


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
