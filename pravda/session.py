"""Browser capture orchestration: ``snapshot()``.

This module owns the Playwright driver, the remote browser connection, the
isolated recording context, and the database persistence of a capture. The
page-level evidence extraction (navigate, wait, screenshot/HTML/text, download
recovery) lives in :mod:`pravda.capture`; here we wire those pieces into a
single public entry point:

* :func:`snapshot` — connect, set up a recording context, capture, finalize,
  persist, all under a wall-clock breaker so a wedged stage becomes a bounded,
  persisted failure. Without ``drive`` it uses the default behavior (navigate to the
  normal ``load`` state); with ``drive`` the caller pilots the recording page
  itself — owning the initial navigation and any readiness or interaction it
  needs — and Pravda captures whatever state it leaves behind.

There is no readiness-condition abstraction. The default (no ``drive``) is a
fixed internal readiness (navigate to commit, then wait for the normal
``load`` state); callers needing anything else pass a ``drive`` callback and
use Playwright directly on the recording page. All configuration is read from
the environment (``BROWSER_WS_URL``, ``DATABASE_URL``,
``STORAGE_BASE_PATH``); there are no constructor overrides. Pravda owns its
database sessions: every attempt (success, partial, or failure) is committed
through :func:`pravda.db.async_session` and returned as a public
:class:`~pravda.snapshots.Snapshot`.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    Page,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from pravda.capture import CaptureResult, capture_current, capture_page
from pravda.db import SnapshotRecord, async_session
from pravda.http_archive import capture_http_archive
from pravda.snapshots import Snapshot, from_record

logger = logging.getLogger(__name__)

BROWSER_CHANNEL = "chrome"
BROWSER_WS_URL = os.environ["BROWSER_WS_URL"]

# Wall-clock budget for the whole capture pipeline (connect -> setup ->
# capture -> context.close -> HAR). capture_page bounds the page interactions
# internally, and the stages below add tighter inner bounds for the calls
# that reach the remote browser server over a pipe. This outer budget is the
# breaker of last resort: it catches a stage that eludes its inner bound —
# notably page capture — converting a silent hang into a bounded failure.
SNAPSHOT_TIMEOUT_S = 75

# Timeout for the handshake with the remote browser server (connect + launch).
# Playwright defaults connect's timeout to 0 (no timeout); bounding it makes a
# dead server fail fast as a PlaywrightError instead of waiting on the
# wall-clock budget.
CONNECT_TIMEOUT_MS = 10_000

# Combined budget for browser.new_context() + context.new_page(). Both reach
# the remote server over a pipe; bounding them catches a wedge before a page
# even exists.
SETUP_TIMEOUT_S = 10

# Budget for unpacking the HAR zip and storing every recorded body, as one
# stage. Individual normal-artifact writes (rendered HTML, plaintext,
# screenshot, downloaded file) are bounded separately inside capture_page.
# On timeout the page evidence is kept and the HAR is discarded.
HAR_PROCESSING_TIMEOUT_S = 20

# Budget for BrowserContext.close(), which flushes the HAR zip. close() has no
# Playwright timeout argument; bounding it preserves the page evidence already
# captured and discards an incomplete HAR instead of hanging.
CONTEXT_CLOSE_TIMEOUT_S = 10

# Budget for the forced browser.close() cleanup that runs after capture is
# finalized. browser.close() has no Playwright timeout argument; bounding it
# guarantees cleanup cannot wedge the caller. A timeout here is logged as an
# operational warning only and never alters the finalized snapshot.
BROWSER_CLOSE_TIMEOUT_S = 5

# Budget to wait for a download event when a navigation handed off to Chrome's
# downloader (the page settles at about:blank before the event fires). Matches
# the default download-recovery window.
DOWNLOAD_TIMEOUT_S = 15


def _launch_options_header() -> dict[str, str]:
    """Encode the Chrome launch options for the WebSocket server header."""
    return {
        "x-playwright-launch-options": json.dumps(
            {"channel": BROWSER_CHANNEL, "headless": False}
        ),
    }


async def _connect(playwright) -> Browser:
    """Connect to the remote browser server under a bounded handshake."""
    return await playwright.chromium.connect(
        BROWSER_WS_URL,
        # connect's timeout defaults to 0 (no timeout). Bound the handshake so
        # a dead server fails fast as a PlaywrightError instead of waiting on
        # the wall-clock budget.
        timeout=CONNECT_TIMEOUT_MS,
        headers=_launch_options_header(),
    )


async def _finalize_capture(
    context: BrowserContext,
    result: CaptureResult,
    http_archive_path: Path | None,
    url: str,
    stages: dict[str, float],
) -> tuple[CaptureResult, dict | None]:
    """Close the context (flushing the HAR) and unpack the archive.

    ``context.close()`` has no Playwright timeout, so bound it. If it wedges,
    the HAR it would flush is incomplete and must be discarded — but the page
    evidence ``capture_page``/``capture_current`` already returned is kept,
    with a fatal error recorded so the snapshot is not mistaken for a success.
    HAR processing follows the same policy on its own timeout.

    *url* is the page URL (for logging). Returns the (possibly error-amended)
    result and the HAR manifest, or ``None`` when navigation did not commit,
    no archive was recorded, or the archive was discarded. *stages* records
    the ``close`` and ``har`` durations for the completion log.
    """
    stage_start = time.monotonic()
    try:
        async with asyncio.timeout(CONTEXT_CLOSE_TIMEOUT_S):
            await context.close()
    except asyncio.TimeoutError:
        stages["close"] = time.monotonic() - stage_start
        logger.error(
            "context.close exceeded %ss budget for %s; discarding HAR",
            CONTEXT_CLOSE_TIMEOUT_S,
            url,
        )
        return (
            replace(
                result,
                error=(
                    f"browser context close exceeded {CONTEXT_CLOSE_TIMEOUT_S}s "
                    f"budget; http archive discarded"
                ),
            ),
            None,
        )
    stages["close"] = time.monotonic() - stage_start

    # The HAR is written when the context closes. Unpack it only when
    # navigation committed — otherwise it holds no useful evidence. Bodies are
    # stored under final_url's hostname prefix so every artifact shares the
    # one prefix exposed to callers; the manifest itself is returned to
    # persist inline as JSON.
    if (
        result.http_status is None
        or http_archive_path is None
        or (not http_archive_path.exists())
    ):
        return result, None

    http_archive: dict | None = None
    stage_start = time.monotonic()
    try:
        async with asyncio.timeout(HAR_PROCESSING_TIMEOUT_S):
            http_archive = await capture_http_archive(
                http_archive_path, result.final_url, download=result.download
            )
    except asyncio.TimeoutError:
        stages["har"] = time.monotonic() - stage_start
        logger.error(
            "HAR processing exceeded %ss budget for %s; discarding HAR",
            HAR_PROCESSING_TIMEOUT_S,
            url,
        )
        return (
            replace(
                result,
                error=(
                    f"HAR processing exceeded {HAR_PROCESSING_TIMEOUT_S}s "
                    f"budget; http archive discarded"
                ),
            ),
            None,
        )
    stages["har"] = time.monotonic() - stage_start
    return result, http_archive


async def _close_browser(browser: Browser) -> None:
    """Force-close a connected browser under a bounded timeout.

    ``browser.close()`` clears contexts and disconnects from the remote server
    (analogous to force-quitting); it has no timeout argument, so bound it.
    Cleanup runs after the snapshot is finalized, so a timeout or error here
    is logged as an operational warning only and never alters the finalized
    snapshot's success or error state.
    """
    try:
        async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT_S):
            await browser.close()
    except asyncio.TimeoutError:
        logger.warning(
            "browser.close exceeded %ss budget; cleanup abandoned",
            BROWSER_CLOSE_TIMEOUT_S,
        )
    except PlaywrightError as error:
        logger.warning("browser.close failed: %s", error.message)


async def _persist_snapshot(
    url: str,
    result: CaptureResult,
    http_archive: dict | None,
    stages: dict[str, float],
) -> Snapshot:
    """Map captured evidence onto a ``SnapshotRecord`` row and commit it.

    Pravda owns its database session: the row is added and committed through
    :func:`pravda.db.async_session` (not a caller-supplied session), so a
    committed attempt is immediately visible to every reader of the shared
    database — including :func:`pravda.snapshots.snapshots`. Returns the public
    :class:`~pravda.snapshots.Snapshot` for the committed row. A database
    failure that prevents the commit propagates to the caller.
    """
    record = SnapshotRecord(
        url=url,
        final_url=result.final_url,
        http_status=result.http_status,
        error=result.error,
        plaintext=result.plaintext,
        rendered_html=result.rendered_html,
        screenshot=result.screenshot,
        http_archive=http_archive,
    )
    stage_start = time.monotonic()
    async with async_session() as session:
        session.add(record)
        await session.commit()
    stages["commit"] = time.monotonic() - stage_start
    logger.info(
        "Captured %s id=%s: status=%s http_archive=%s error=%s timings=%s",
        record.url,
        record.id,
        record.http_status,
        record.http_archive is not None,
        record.error,
        " ".join(f"{name}={duration:.2f}s" for name, duration in stages.items()),
    )
    return from_record(record)


# An async callback that pilots a freshly created recording page: it owns
# every navigation and interaction (selectors, load states, clicks, form
# fills), using Playwright directly. Passed to :func:`snapshot` as ``drive``.
DriveCallback = Callable[[Page, str], Awaitable[None]]


async def _drive_capture(page: Page, url: str, drive: DriveCallback) -> CaptureResult:
    """Run the caller's *drive* callback against *page*, then capture the
    resulting current page state.

    The latest main-frame document response and the first download are
    observed for the duration of the callback so :func:`capture_current` can
    record them. The callback owns every navigation and interaction —
    including catching a ``Download is starting`` error from a PDF-like
    navigation so the download can be recovered, just as the default path does
    internally.

    A navigation that handed off to Chrome's downloader (e.g. a PDF) leaves
    the page at ``about:blank``; if the download event has not fired by the
    time the callback returns it is awaited (bounded) so the download becomes
    the captured subject. Returns the evidence as a ``CaptureResult``; the
    caller finalizes and persists.

    A Playwright error or timeout raised by *drive* propagates to the failure
    handling in :func:`snapshot` (persisted as a failed attempt); any other
    exception raised by *drive* propagates to the caller untouched once the
    listeners are removed.
    """
    navigation_status: int | None = None
    download: Download | None = None
    loop = asyncio.get_running_loop()
    download_future: asyncio.Future[Download] = loop.create_future()
    main_frame = page.main_frame

    def on_download(received: Download) -> None:
        nonlocal download
        if download is None:
            download = received
        if not download_future.done():
            download_future.set_result(received)

    def on_response(response) -> None:
        nonlocal navigation_status
        if response.request.is_navigation_request() and response.frame is main_frame:
            navigation_status = response.status

    page.on("download", on_download)
    page.on("response", on_response)
    try:
        await drive(page, url)

        if page.url == "about:blank" and navigation_status is None and download is None:
            raise ValueError(
                "drive returned before navigating the page; "
                "call page.goto(...) before returning"
            )

        # A navigation that handed off to Chrome's downloader (e.g. a PDF)
        # leaves the page at about:blank; the download event may follow the
        # callback's return. Keep the listener installed while awaiting it so
        # the download becomes the captured subject.
        if download is None and page.url == "about:blank":
            try:
                async with asyncio.timeout(DOWNLOAD_TIMEOUT_S):
                    download = await download_future
            except asyncio.TimeoutError:
                logger.warning("Download event did not fire for %s", url)

        final_url = download.url if download is not None else page.url
        return await capture_current(page, final_url, navigation_status, download)
    finally:
        page.remove_listener("download", on_download)
        page.remove_listener("response", on_response)


async def snapshot(url: str, *, drive: DriveCallback | None = None) -> Snapshot:
    """Capture a snapshot of *url* and persist it.

    Without *drive* this is the default behavior: connect to the remote browser,
    set up an isolated context recording a HAR, navigate (waiting for the
    normal ``load`` state), capture the page evidence, flush and process the
    HAR, then commit the result.

    With *drive*, Pravda still owns the browser connection, the recording
    context, the page, and persistence — but the caller drives the page.
    Pravda creates the recording page, hands it (and *url*) to
    ``await drive(page, url)``, then captures the resulting current page
    state. The callback has complete control, including the initial
    navigation and any readiness or interaction it needs (selectors, load
    states, clicks, form fills); use Playwright directly on *page*. A
    navigation that hands off to a download (e.g. a PDF) is recovered too:
    ``goto`` raises ``Download is starting``, so catch it inside *drive* and
    Pravda observes the download event and captures the bytes, as the default
    path does internally.

    The whole pipeline runs under a wall-clock breaker so a wedged stage
    becomes a bounded failure. Browser/navigation/timeout failures —
    including Playwright errors and timeouts raised from *drive* — are still
    persisted as :class:`~pravda.snapshots.Snapshot` rows (with ``error`` set
    and no evidence); a database failure that prevents the commit propagates.
    An arbitrary (non-Playwright, non-timeout) exception raised by *drive* is
    not turned into a snapshot failure: Pravda cleans up and re-raises it to
    the caller.

    The recorded ``url`` is *url* (the subject the caller asked to capture);
    ``final_url`` reflects where the page actually landed (``page.url``, or
    the download URL when a download was captured). Returns the public
    :class:`~pravda.snapshots.Snapshot` for the attempt.
    """
    logger.info("Capturing %s", url)

    http_archive_dir = Path(tempfile.mkdtemp())
    stages: dict[str, float] = {}
    timeout_error: str | None = None
    # Defaults for a capture that produced no evidence; the pipeline below
    # overwrites these on success or partial success.
    result = CaptureResult(
        http_status=None,
        error=None,
        final_url=None,
        plaintext=None,
        rendered_html=None,
        screenshot=None,
        download=None,
    )
    http_archive: dict | None = None
    try:
        async with async_playwright() as playwright:
            # async_playwright() is the *outer* context manager so its teardown
            # is guaranteed to run once the pipeline below resolves (success, a
            # handled failure, or the outer breaker). Its teardown kills the
            # local driver subprocess, which is expected to be fast — but being
            # outside the deadline does not by itself prove it cannot hang, so
            # we also explicitly close the connected browser under a bounded
            # timeout in the finally below rather than relying on teardown
            # alone.
            browser: Browser | None = None
            try:
                # Whole-pipeline wall-clock breaker. Inner stages carry their
                # own tighter bounds (connect, setup, context.close, HAR);
                # capture_page bounds the default page interactions internally,
                # while a ``drive`` callback is bounded by this outer budget.
                # It is also the breaker of last resort for a stage that eludes
                # its inner bound (notably page capture), converting a silent
                # hang into a bounded failure.
                async with asyncio.timeout(SNAPSHOT_TIMEOUT_S):
                    stage_start = time.monotonic()
                    browser = await _connect(playwright)
                    stages["connect"] = time.monotonic() - stage_start

                    # Record a HAR of all network activity, with response
                    # bodies stored as separate entries inside a zip archive
                    # (record_har_content="attach" + a .zip path). The file is
                    # flushed when the context closes.
                    http_archive_path = http_archive_dir / "record.zip"
                    stage_start = time.monotonic()
                    try:
                        async with asyncio.timeout(SETUP_TIMEOUT_S):
                            context = await browser.new_context(
                                record_har_path=str(http_archive_path),
                                record_har_content="attach",
                            )
                            page = await context.new_page()
                    except asyncio.TimeoutError:
                        timeout_error = (
                            f"context/page setup exceeded {SETUP_TIMEOUT_S}s budget"
                        )
                        raise
                    stages["setup"] = time.monotonic() - stage_start

                    stage_start = time.monotonic()
                    if drive is None:
                        result = await capture_page(page, url)
                    else:
                        result = await _drive_capture(page, url, drive)
                    stages["capture"] = time.monotonic() - stage_start

                    # Close the context (flushing the HAR) and unpack the
                    # archive. Both carry fatal-evidence semantics on timeout:
                    # the page evidence already captured is kept, a precise
                    # error is recorded, and the (potentially incomplete) HAR
                    # is discarded.
                    result, http_archive = await _finalize_capture(
                        context,
                        result,
                        http_archive_path,
                        url,
                        stages,
                    )
            finally:
                # Disconnect the browser on every path once one was connected.
                # browser.close() clears contexts and disconnects from the
                # remote server; it is bounded so cleanup cannot wedge the
                # caller, and a cleanup timeout/failure is an operational
                # warning only that never alters the finalized snapshot.
                if browser is not None:
                    await _close_browser(browser)
    except PlaywrightError as error:
        # Couldn't even reach the browser — record an empty, failed result.
        logger.error("Browser error for %s: %s", url, error.message)
        result = CaptureResult(
            http_status=None,
            error=error.message,
            final_url=None,
            plaintext=None,
            rendered_html=None,
            screenshot=None,
            download=None,
        )
        http_archive = None
    except asyncio.TimeoutError:
        # The outer breaker (or the setup budget) tripped. timeout_error names
        # the setup stage when that fired; otherwise a stage that eluded its
        # inner bound hit the wall-clock budget. No partial evidence is
        # preserved here — the dedicated context.close/HAR handlers in
        # _finalize_capture own the partial-evidence policy for those stages.
        message = timeout_error or (
            f"snapshot exceeded {SNAPSHOT_TIMEOUT_S}s wall-clock budget"
        )
        logger.error("%s for %s", message, url)
        result = CaptureResult(
            http_status=None,
            error=message,
            final_url=None,
            plaintext=None,
            rendered_html=None,
            screenshot=None,
            download=None,
        )
        http_archive = None
    finally:
        shutil.rmtree(http_archive_dir, ignore_errors=True)

    return await _persist_snapshot(url, result, http_archive, stages)
