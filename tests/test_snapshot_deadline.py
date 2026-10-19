"""Liveness and wall-clock bounds for ``Pravda.snapshot``."""

import asyncio
import os
from dataclasses import replace

import pytest
from playwright.async_api import BrowserContext, BrowserType
from sqlalchemy.ext.asyncio import AsyncSession

from pravda import InfrastructureError, Liveness, Pravda, PravdaConfig, Timeouts
from tests.helpers import fulfill_html


async def _drive_example(page, url):
    await page.route(url, fulfill_html)
    await page.goto(url, wait_until="load")


def _pravda(sessionmaker, tmp_path, liveness=None, **budgets: float) -> Pravda:
    """A client with the given budget fields shrunk from the defaults."""
    config = PravdaConfig(
        browser_ws_url=os.environ["PRAVDA_TEST_BROWSER_WS_URL"],
        storage_base_path=str(tmp_path),
        timeouts=replace(Timeouts(), dom_settle_quiet=0.05, **budgets),
        liveness=liveness or Liveness(),
    )
    return Pravda(config, sessionmaker)


@pytest.mark.asyncio
async def test_snapshot_drive_timeout_persists_failed_attempt(sessionmaker, tmp_path):
    pravda = _pravda(sessionmaker, tmp_path, drive=0.2)

    async def drive(page, url):
        await asyncio.sleep(5)

    snapshot = await pravda.snapshot("https://example.com", drive=drive)

    assert "drive" in snapshot.error.lower()
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_snapshot_wall_clock_timeout_persists_failed_attempt(
    sessionmaker, tmp_path
):
    """The wall-clock breaker expires while no inner budget has."""
    pravda = _pravda(sessionmaker, tmp_path, snapshot=0.2)

    async def drive(page, url):
        await asyncio.sleep(5)

    snapshot = await pravda.snapshot("https://example.com", drive=drive)

    assert "wall-clock" in snapshot.error
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_setup_timeout_propagates_without_persisting(
    sessionmaker, tmp_path, monkeypatch
):
    pravda = _pravda(sessionmaker, tmp_path, liveness=Liveness(setup=0.2))

    async def slow_connect(self, url, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(BrowserType, "connect", slow_connect)

    with pytest.raises(InfrastructureError, match="setup"):
        await pravda.snapshot("https://example.com", drive=_drive_example)

    assert await pravda.snapshots("https://example.com") == []


@pytest.mark.asyncio
async def test_context_close_timeout_raises_without_persisting(
    sessionmaker, tmp_path, monkeypatch
):
    pravda = _pravda(sessionmaker, tmp_path, liveness=Liveness(close=0.2))

    async def slow_close(self, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(BrowserContext, "close", slow_close)
    with pytest.raises(InfrastructureError, match="context close"):
        await pravda.snapshot("https://example.com", drive=_drive_example)

    assert await pravda.snapshots("https://example.com") == []


def test_liveness_deadlines_must_be_positive():
    with pytest.raises(ValueError, match="setup"):
        Liveness(setup=0)


@pytest.mark.asyncio
async def test_snapshot_wedged_persistence_bounded_by_caller(
    sessionmaker, tmp_path, monkeypatch
):
    """A wedged database hangs until the caller cancels, persisting nothing."""
    pravda = _pravda(sessionmaker, tmp_path)

    commit_started = asyncio.Event()

    async def hanging_commit(self):
        commit_started.set()
        await asyncio.sleep(5)

    monkeypatch.setattr(AsyncSession, "commit", hanging_commit)

    capture = asyncio.create_task(
        pravda.snapshot("https://example.com", drive=_drive_example)
    )
    # Reach the wedge first, so the bound below cancels the commit —
    # not a capture that simply was not done yet.
    await asyncio.wait_for(commit_started.wait(), timeout=10)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(capture, timeout=0.2)

    assert await pravda.snapshots("https://example.com") == []


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
