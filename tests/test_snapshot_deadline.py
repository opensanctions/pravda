"""Deadline-architecture tests for ``Pravda.snapshot``.

snapshot() is bounded phase by phase rather than by a single outer breaker
(see the deadline note in ``pravda.pravda``). These tests cover the behaviors
that the redesign guarantees:

* a capture-phase timeout (startup / setup / drive) persists a failed attempt;
* evidence already captured is preserved through a later finalize timeout
  (no outer breaker erases it);
* a wedged database commit is a database failure: the timeout propagates and
  nothing is pretended persisted;
* browser cleanup and driver teardown are bounded and best-effort, and never
  alter the finalized state;
* cancellation (BaseException) is never caught.

Where practical these drive the public ``Pravda.snapshot`` against the real
browser and test database; the cleanup/teardown bounds are also exercised
directly against the helpers that enforce them (deterministic and fast), as
the existing finalize tests do.
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
    """A drive callback that exceeds its budget is a capture-phase timeout:
    the attempt is persisted as a failed snapshot with no evidence."""
    monkeypatch.setattr(capture_module, "DRIVE_TIMEOUT_S", 0.2)

    async def drive(page, url):
        # User code that hangs; the drive budget cancels it well before 5s.
        await asyncio.sleep(5)

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

    # Committed through Pravda's own session — visible to snapshots().
    history = await pravda.snapshots("https://example.com")
    assert any(item.id == snapshot.id for item in history)


@pytest.mark.asyncio
async def test_snapshot_setup_timeout_persists_failed_attempt(
    pravda: Pravda, monkeypatch
):
    """A context/page setup that exceeds its budget is a capture-phase
    timeout: the attempt is persisted as a failed snapshot with no evidence."""
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
    """A Playwright driver startup that wedges is bounded explicitly (we no
    longer rely on an unbounded context-manager teardown): it is a
    pre-capture browser/infra failure, persisted as an empty failed attempt."""

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
    """A context.close timeout during finalization does not erase the page
    evidence already captured: the snapshot is persisted with the artifacts,
    a finalization error composed onto the (clean) capture, and no HAR.

    This is the core guarantee of the phase-budget design — there is no outer
    breaker whose budget can trip after evidence is captured and replace it
    with an empty result.
    """
    monkeypatch.setattr(pravda_module, "CONTEXT_CLOSE_TIMEOUT_S", 0.01)

    async def slow_close(self, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(BrowserContext, "close", slow_close)

    snapshot = await pravda.snapshot("https://example.com", drive=_drive_example)

    assert snapshot.http_status == 200
    assert snapshot.final_url == "https://example.com/"
    # Page evidence survived the finalize timeout.
    assert snapshot.rendered_html.endswith(".html")
    assert snapshot.plaintext.endswith(".txt")
    # The finalize failure is recorded, and the (unflushed) HAR is dropped.
    assert snapshot.error is not None
    assert "close" in snapshot.error.lower()
    assert snapshot.http_archive is None

    history = await pravda.snapshots("https://example.com")
    assert any(item.id == snapshot.id for item in history)


# --- persistence is bounded; a wedged commit propagates -----------------


@pytest.mark.asyncio
async def test_snapshot_persistence_timeout_propagates(pravda: Pravda, monkeypatch):
    """A wedged database commit is a database failure: the persistence budget
    trips, the timeout propagates to the caller, and nothing is pretended to
    have been persisted."""
    monkeypatch.setattr(pravda_module, "PERSIST_TIMEOUT_S", 0.2)

    async def hanging_commit(self):
        await asyncio.sleep(5)

    monkeypatch.setattr(AsyncSession, "commit", hanging_commit)

    with pytest.raises(asyncio.TimeoutError):
        await pravda.snapshot("https://example.com", drive=_drive_example)

    # Nothing was committed, so history is empty. (snapshots() uses execute,
    # not commit, so the patch does not affect the read.)
    assert await pravda.snapshots("https://example.com") == []


# --- cleanup / driver teardown are bounded and best-effort --------------


class _WedgedClose:
    """A browser/driver stand-in whose close/stop never returns on its own."""

    async def close(self):
        await asyncio.sleep(5)

    async def stop(self):
        await asyncio.sleep(5)


@pytest.mark.asyncio
async def test_close_browser_is_bounded(monkeypatch):
    """browser.close() cleanup cannot exceed its budget: a wedged close is
    cancelled at the budget and the helper returns (logged as a warning)."""
    monkeypatch.setattr(pravda_module, "BROWSER_CLOSE_TIMEOUT_S", 0.2)

    start = time.monotonic()
    await pravda_module._close_browser(_WedgedClose())
    elapsed = time.monotonic() - start

    # Bound far below the 5s the close would otherwise sleep.
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_stop_playwright_is_bounded(monkeypatch):
    """playwright.stop() teardown cannot exceed its budget: a wedged stop is
    cancelled at the budget and the helper returns (logged as a warning)."""
    monkeypatch.setattr(pravda_module, "PLAYWRIGHT_STOP_TIMEOUT_S", 0.2)

    start = time.monotonic()
    await pravda_module._stop_playwright(_WedgedClose(), {})
    elapsed = time.monotonic() - start

    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_snapshot_slow_browser_cleanup_is_bounded_and_keeps_evidence(
    pravda: Pravda, monkeypatch
):
    """End-to-end: a browser.close that wedges during cleanup cannot block the
    snapshot or alter its finalized state — cleanup is best-effort and bounded,
    so the evidence is persisted intact and the call returns within the budget.
    """

    async def slow_close(self, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(pravda_module, "BROWSER_CLOSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(Browser, "close", slow_close)

    start = time.monotonic()
    snapshot = await pravda.snapshot("https://example.com", drive=_drive_example)
    elapsed = time.monotonic() - start

    # Evidence persisted intact despite the wedged cleanup.
    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.rendered_html.endswith(".html")
    assert snapshot.plaintext.endswith(".txt")
    assert snapshot.http_archive is not None
    # Cleanup was bounded: the call returned far sooner than the 5s sleep.
    assert elapsed < 4.0


# --- cancellation is never caught ---------------------------------------


@pytest.mark.asyncio
async def test_snapshot_cancellation_propagates(pravda: Pravda):
    """Cancelling an in-flight snapshot propagates CancelledError rather than
    swallowing it as a persisted failed attempt. No phase handler catches
    BaseException."""
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

    # Cancellation escaped before persistence, so nothing was committed.
    assert await pravda.snapshots("https://example.com") == []
