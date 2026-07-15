"""Phase-by-phase deadline behavior for ``Pravda.snapshot``.

snapshot() is bounded phase by phase rather than by a single outer breaker.
These tests cover the guarantees of that design:

* a capture-phase timeout (startup / setup / drive) persists a failed attempt;
* evidence already captured survives a later finalize timeout;
* a wedged database commit propagates rather than pretending to persist;
* driver teardown is bounded and best-effort, never altering finalized state;
* cancellation (BaseException) is never caught.
"""

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
    """Route and navigate to the example fixture, then return."""
    await page.route(url, _fulfill_html)
    await page.goto(url, wait_until="load")


# --- capture-phase timeouts persist a failed attempt ---------------------


@pytest.mark.asyncio
async def test_snapshot_drive_timeout_persists_failed_attempt(
    pravda: Pravda, monkeypatch
):
    """A drive callback that exceeds its budget is persisted as a failed
    snapshot with no evidence."""
    monkeypatch.setattr(capture_module, "DRIVE_TIMEOUT_S", 0.2)

    async def drive(page, url):
        await asyncio.sleep(5)  # hangs; the drive budget cancels it

    snapshot = await pravda.snapshot("https://example.com", drive=drive)

    assert snapshot.url == "https://example.com"
    assert snapshot.error is not None
    assert "drive" in snapshot.error.lower()
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.plaintext is None
    assert snapshot.rendered_html is None
    assert snapshot.screenshot is None
    assert snapshot.http_archive is None

    history = await pravda.snapshots("https://example.com")
    assert any(item.id == snapshot.id for item in history)


@pytest.mark.asyncio
async def test_snapshot_setup_timeout_persists_failed_attempt(
    pravda: Pravda, monkeypatch
):
    """A context/page setup that exceeds its budget is persisted as a failed
    snapshot with no evidence."""
    monkeypatch.setattr(pravda_module, "SETUP_TIMEOUT_S", 0.2)

    async def slow_new_context(self, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(Browser, "new_context", slow_new_context)

    snapshot = await pravda.snapshot("https://example.com", drive=_drive_example)

    assert snapshot.error is not None
    assert "setup" in snapshot.error.lower()
    assert snapshot.http_status is None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.http_archive is None

    history = await pravda.snapshots("https://example.com")
    assert any(item.id == snapshot.id for item in history)


@pytest.mark.asyncio
async def test_snapshot_playwright_startup_timeout_persists_failed_attempt(
    pravda: Pravda, monkeypatch
):
    """A wedged Playwright driver startup is bounded explicitly (not left to
    an unbounded context-manager teardown): a pre-capture browser failure,
    persisted as an empty failed attempt."""

    class _WedgedDriver:
        async def start(self):
            await asyncio.sleep(5)

    monkeypatch.setattr(pravda_module, "PLAYWRIGHT_START_TIMEOUT_S", 0.2)
    monkeypatch.setattr(pravda_module, "async_playwright", lambda: _WedgedDriver())

    snapshot = await pravda.snapshot("https://example.com")

    assert snapshot.error is not None
    assert "playwright startup" in snapshot.error.lower()
    assert snapshot.http_status is None
    assert snapshot.rendered_html is None
    assert snapshot.http_archive is None

    history = await pravda.snapshots("https://example.com")
    assert any(item.id == snapshot.id for item in history)


# --- evidence already captured is preserved on a later timeout -----------


@pytest.mark.asyncio
async def test_snapshot_context_close_timeout_preserves_evidence(
    pravda: Pravda, monkeypatch
):
    """A context.close timeout during finalization does not erase already-
    captured evidence: the snapshot is persisted with the artifacts, a
    finalization error composed onto the capture, and no HAR."""
    monkeypatch.setattr(pravda_module, "CONTEXT_CLOSE_TIMEOUT_S", 0.01)

    async def slow_close(self, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(BrowserContext, "close", slow_close)

    snapshot = await pravda.snapshot("https://example.com", drive=_drive_example)

    assert snapshot.http_status == 200
    assert snapshot.final_url == "https://example.com/"
    assert snapshot.rendered_html.endswith(".html")
    assert snapshot.plaintext.endswith(".txt")
    assert snapshot.error is not None
    assert "close" in snapshot.error.lower()
    assert snapshot.http_archive is None

    history = await pravda.snapshots("https://example.com")
    assert any(item.id == snapshot.id for item in history)


# --- persistence is bounded; a wedged commit propagates -----------------


@pytest.mark.asyncio
async def test_snapshot_persistence_timeout_propagates(pravda: Pravda, monkeypatch):
    """A wedged commit is a database failure: the persistence budget trips,
    the timeout propagates, and nothing is pretended persisted."""
    monkeypatch.setattr(pravda_module, "PERSIST_TIMEOUT_S", 0.2)

    async def hanging_commit(self):
        await asyncio.sleep(5)

    monkeypatch.setattr(AsyncSession, "commit", hanging_commit)

    with pytest.raises(asyncio.TimeoutError):
        await pravda.snapshot("https://example.com", drive=_drive_example)

    # snapshots() reads via execute, not commit, so the patch does not affect it.
    assert await pravda.snapshots("https://example.com") == []


# --- driver teardown is bounded and best-effort -------------------------


class _WedgedStop:
    """A driver stand-in whose stop() never returns on its own."""

    async def stop(self):
        await asyncio.sleep(5)


@pytest.mark.asyncio
async def test_stop_playwright_is_bounded(monkeypatch):
    """playwright.stop() teardown cannot exceed its budget: a wedged stop is
    cancelled at the budget and the helper returns (logged as a warning)."""
    monkeypatch.setattr(pravda_module, "PLAYWRIGHT_STOP_TIMEOUT_S", 0.2)

    start = time.monotonic()
    await pravda_module._stop_playwright(_WedgedStop(), {})
    elapsed = time.monotonic() - start

    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_snapshot_slow_browser_cleanup_is_bounded_and_keeps_evidence(
    pravda: Pravda, monkeypatch
):
    """A browser.close that wedges during cleanup cannot block the snapshot or
    alter its finalized state: cleanup is best-effort and bounded, so evidence
    is persisted intact and the call returns within the budget."""

    async def slow_close(self, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(pravda_module, "BROWSER_CLOSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(Browser, "close", slow_close)

    start = time.monotonic()
    snapshot = await pravda.snapshot("https://example.com", drive=_drive_example)
    elapsed = time.monotonic() - start

    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.rendered_html.endswith(".html")
    assert snapshot.plaintext.endswith(".txt")
    assert snapshot.http_archive is not None
    assert elapsed < 4.0


# --- cancellation is never caught ---------------------------------------


@pytest.mark.asyncio
async def test_snapshot_cancellation_propagates(pravda: Pravda):
    """Cancelling an in-flight snapshot propagates CancelledError rather than
    swallowing it as a persisted failed attempt."""
    started = asyncio.Event()
    block = asyncio.Event()

    async def drive(page, url):
        started.set()
        await block.wait()  # never set: drive blocks until cancelled

    task = asyncio.create_task(pravda.snapshot("https://example.com", drive=drive))
    await started.wait()
    await asyncio.sleep(0)  # let drive reach the blocking await
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert await pravda.snapshots("https://example.com") == []
