"""Browser capture orchestration: one-shot ``snapshot()`` and interactive
``browser()``.

This module owns the Playwright driver, the remote browser connection, the
isolated recording context, and the database persistence of a capture. The
page-level evidence extraction (navigate, wait, screenshot/HTML/text, download
recovery) lives in :mod:`pravda.capture`; here we wire those pieces into two
public entry points:

* :func:`snapshot` — the one-shot path. Connect, set up a recording context,
  capture, finalize, persist. The whole pipeline runs under a wall-clock
  breaker so a wedged stage becomes a bounded, persisted failure.

* :func:`browser` — an async context manager yielding a :class:`BrowserSession`
  that owns the driver/connection/context/page and observes navigation status
  and downloads. The caller drives ``session.page`` freely — using Playwright
  directly for selectors, load states, clicks, form fills — and calls
  ``session.snapshot()`` (terminal) to capture and persist.

There is no readiness-condition abstraction. The one-shot path uses a fixed
internal default (navigate to commit, then wait for the normal ``load``
state); callers needing anything else drive the real Playwright page through
``browser()``. All configuration is read from the environment
(``BROWSER_WS_URL``, ``DATABASE_URL``, ``STORAGE_BASE_PATH``); there are no
constructor overrides. Pravda owns its database sessions: every attempt
(success, partial, or failure) is committed through :func:`pravda.db.async_session`
and returned as a public :class:`~pravda.snapshots.Snapshot`.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
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

# Wall-clock budget for the whole one-shot pipeline (connect -> setup ->
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
# even exists. Used by both the one-shot setup and BrowserSession.__aenter__.
SETUP_TIMEOUT_S = 10

# Budget for unpacking the HAR zip and storing every recorded body, as one
# stage. Individual normal-artifact writes (rendered HTML, plaintext,
# screenshot, downloaded file) are bounded separately inside capture_page.
# On timeout the page evidence is kept and the HAR is discarded.
HAR_PROCESSING_TIMEOUT_S = 20

# Budget for BrowserContext.close(), which flushes the HAR zip. close() has no
# Playwright timeout argument; bounding it preserves the page evidence already
# captured and discards an incomplete HAR instead of hanging. Also bounds the
# cleanup-path close in BrowserSession._close().
CONTEXT_CLOSE_TIMEOUT_S = 10

# Budget for the forced browser.close() cleanup that runs after capture is
# finalized. browser.close() has no Playwright timeout argument; bounding it
# guarantees cleanup cannot wedge the caller. A timeout here is logged as an
# operational warning only and never alters the finalized snapshot.
BROWSER_CLOSE_TIMEOUT_S = 5

# Budget to wait for a download event when an interactive navigation handed off
# to Chrome's downloader (the page settles at about:blank before the event
# fires). Matches the one-shot download-recovery window.
DOWNLOAD_TIMEOUT_S = 15


class PravdaError(Exception):
    """A capture session was used in an unsupported way.

    Raised when a terminal :class:`BrowserSession` is used again (a second
    ``snapshot()`` call, ``.page`` access after the session closed, or
    ``snapshot()`` before any navigation).
    """


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


async def snapshot(url: str) -> Snapshot:
    """Capture a one-shot snapshot of *url* and persist it.

    Connect to the remote browser, set up an isolated context recording a HAR,
    navigate (waiting for the normal ``load`` state), capture the page
    evidence, flush and process the HAR, then commit the result.

    The whole pipeline runs under a wall-clock breaker so a wedged stage
    becomes a bounded failure. Browser/navigation/timeout failures are still
    persisted as :class:`~pravda.snapshots.Snapshot` rows (with ``error`` set
    and no evidence); a database failure that prevents the commit propagates.

    Returns the public :class:`~pravda.snapshots.Snapshot` for the attempt.
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
                # capture_page bounds the page interactions internally. This
                # outer budget is the breaker of last resort for a stage that
                # eludes its inner bound (notably page capture), converting a
                # silent hang into a bounded failure.
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
                    result = await capture_page(page, url)
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


class BrowserSession:
    """An interactive capture session owning a Playwright driver, a remote
    browser connection, an isolated browser context recording a HAR, and one
    real :class:`~playwright.async_api.Page`.

    Construct one with :func:`browser` and drive it as an async context
    manager::

        async with pravda.browser() as session:
            page = session.page
            await page.goto("https://example.com", wait_until="commit")
            await page.wait_for_selector(".results")
            snapshot = await session.snapshot()

    The caller drives readiness directly through Playwright (selectors, load
    states, clicks, form fills). Navigation status and downloads are observed
    for the whole session, so a single session may span multiple navigations;
    only the latest main-frame navigation is recorded (iframe document
    responses are excluded). :meth:`snapshot` is **terminal**: it captures the
    current page state, closes the context to flush the HAR, processes and
    persists the evidence through Pravda's own database session, and returns
    the public frozen :class:`~pravda.snapshots.Snapshot`. After it returns the
    session is terminal — ``.page`` and further ``snapshot()`` calls raise
    :class:`PravdaError`. Browser and driver cleanup completes when the context
    manager exits; that cleanup is safe and idempotent.

    All hidden state lives on the session object; there is no global
    page-to-session registry.
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._main_frame = None
        self._http_archive_dir: Path | None = None
        self._http_archive_path: Path | None = None
        self._terminal = False
        # Observed session state, populated by the listeners installed in
        # _open() and read by snapshot().
        self._navigation_status: int | None = None
        self._download: Download | None = None
        self._download_future: asyncio.Future[Download] | None = None
        self._on_download = None
        self._on_response = None

    @property
    def page(self) -> Page:
        """The live Playwright page to drive.

        Raises :class:`PravdaError` once the session is terminal (after
        :meth:`snapshot` has captured and closed the context).
        """
        if self._terminal:
            raise PravdaError(
                "BrowserSession is terminal (snapshot() was called); "
                "open a new browser() session to capture again."
            )
        if self._page is None:
            raise PravdaError("BrowserSession has no page (not entered).")
        return self._page

    async def __aenter__(self) -> "BrowserSession":
        await self._open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._close()

    async def _open(self) -> None:
        """Start the driver, connect, build the recording context/page, and
        install observation. On any failure, tear down what was opened and
        re-raise — the caller's ``async with`` never yields a half-built
        session."""
        self._playwright = await async_playwright().start()
        try:
            self._browser = await _connect(self._playwright)
            self._http_archive_dir = Path(tempfile.mkdtemp())
            self._http_archive_path = self._http_archive_dir / "record.zip"
            async with asyncio.timeout(SETUP_TIMEOUT_S):
                self._context = await self._browser.new_context(
                    record_har_path=str(self._http_archive_path),
                    record_har_content="attach",
                )
                self._page = await self._context.new_page()
                self._main_frame = self._page.main_frame
            self._install_observers()
        except BaseException:
            await self._close()
            raise

    def _install_observers(self) -> None:
        """Watch the latest main-frame navigation response and any download.

        Only main-frame navigations count: ``response.frame is main_frame``
        excludes iframe document responses, so the recorded status reflects
        the page the caller actually drove. The first download of the session
        is kept (later ones are ignored) and also resolves the download future
        so :meth:`snapshot` can await a navigation that handed off to Chrome's
        downloader (e.g. a PDF) before its event fires.
        """
        loop = asyncio.get_running_loop()
        self._download_future = loop.create_future()

        def on_download(download) -> None:
            if self._download is None:
                self._download = download
            future = self._download_future
            if future is not None and not future.done():
                future.set_result(download)

        def on_response(response) -> None:
            if (
                response.request.is_navigation_request()
                and response.frame is self._main_frame
            ):
                self._navigation_status = response.status

        self._on_download = on_download
        self._on_response = on_response
        page = self._page
        page.on("download", on_download)
        page.on("response", on_response)

    async def snapshot(self) -> Snapshot:
        """Terminal capture: freeze the current page state and persist it.

        Captures the page-content artifacts from the current DOM (or recovers
        a download's bytes when the navigation handed off to Chrome's
        downloader), closes the context to flush the HAR, processes and
        persists the evidence through Pravda's own database session, and
        returns the public :class:`~pravda.snapshots.Snapshot`.

        The recorded URL is the page's current URL (``page.url``) — the caller
        drove every navigation, so that is the honest subject of the evidence.
        When a download fired, the download's URL is used instead and the
        blank page's artifacts are skipped, matching the one-shot PDF path.

        Raises :class:`PravdaError` if called before any navigation (the page
        is still ``about:blank`` — there is no meaningful evidence to capture)
        or after the session is already terminal. After a successful return the
        session is terminal; ``.page`` and further ``snapshot()`` calls raise.
        Exiting the context manager afterwards is safe (cleanup is bounded and
        idempotent).
        """
        if self._terminal:
            raise PravdaError(
                "BrowserSession.snapshot() was already called; the session is "
                "terminal. Open a new browser() session to capture again."
            )
        # Refuse to capture a page that was never navigated: it is still at
        # about:blank and would persist as bogus "successful" evidence. A
        # download also counts as a navigation (its response arrives via the
        # listener before the goto that handed off raises), so the download
        # path is allowed through. Checked before terminalizing so no capture
        # or persistence runs.
        if self._navigation_status is None and self._download is None:
            raise PravdaError(
                "BrowserSession.snapshot() called before any navigation; "
                "navigate the page (e.g. await page.goto(...)) before capturing."
            )

        page = self._page
        context = self._context
        if page is None or context is None:
            raise PravdaError("BrowserSession has no page (not entered).")

        download = self._download
        # If the main frame is at about:blank despite an observed navigation,
        # the navigation handed off to Chrome's downloader (e.g. a PDF) and the
        # download event may not have fired yet — await it so the download is
        # the captured subject (matching the one-shot PDF path).
        if download is None and page.url == "about:blank":
            download = await self._await_download()
        final_url = download.url if download is not None else page.url

        self._terminal = True
        stages: dict[str, float] = {}
        stage_start = time.monotonic()
        result = await capture_current(
            page, final_url, self._navigation_status, download
        )
        stages["capture"] = time.monotonic() - stage_start

        result, http_archive = await _finalize_capture(
            context,
            result,
            self._http_archive_path,
            final_url,
            stages,
        )

        return await _persist_snapshot(final_url, result, http_archive, stages)

    async def _await_download(self) -> Download | None:
        """Wait for the download event of a navigation that handed off as a
        download.

        Used by :meth:`snapshot` when the main frame is at ``about:blank``:
        the navigation response already arrived (so the status is known) but
        the download event follows. Returns the download or ``None`` on
        timeout.
        """
        future = self._download_future
        if future is None:
            return None
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT_S):
                return await future
        except asyncio.TimeoutError:
            page_url = self._page.url if self._page is not None else "?"
            logger.warning("Download event did not fire for %s", page_url)
            return None

    async def _close(self) -> None:
        """Tear down the session. Idempotent and safe after a terminal
        :meth:`snapshot`: every step is guarded and nulls what it tears down,
        so a second call (e.g. ``__aexit__`` after a successful snapshot, or
        after ``_open`` failed and cleaned up) is a no-op.

        The context is closed whenever one is still held. A terminal
        snapshot's :func:`_finalize_capture` already closed it; the redundant
        close raises (caught below) — both paths end with the context released,
        so cleanup is robust even if capture/finalization/persistence raised.
        """
        page = self._page
        if page is not None:
            if self._on_download is not None:
                try:
                    page.remove_listener("download", self._on_download)
                except PlaywrightError:
                    pass
                self._on_download = None
            if self._on_response is not None:
                try:
                    page.remove_listener("response", self._on_response)
                except PlaywrightError:
                    pass
                self._on_response = None
        self._page = None
        self._main_frame = None

        context = self._context
        if context is not None:
            try:
                async with asyncio.timeout(CONTEXT_CLOSE_TIMEOUT_S):
                    await context.close()
            except asyncio.TimeoutError:
                logger.warning(
                    "context.close exceeded %ss budget during cleanup; abandoning",
                    CONTEXT_CLOSE_TIMEOUT_S,
                )
            except PlaywrightError as error:
                # Already closed by snapshot()'s _finalize_capture — expected
                # on the terminal path; other failures are operational only.
                logger.debug("context.close during cleanup: %s", error.message)
            self._context = None

        browser = self._browser
        if browser is not None:
            await _close_browser(browser)
            self._browser = None

        playwright = self._playwright
        if playwright is not None:
            try:
                await playwright.stop()
            except PlaywrightError as error:
                logger.warning("playwright driver stop failed: %s", error.message)
            self._playwright = None

        if self._http_archive_dir is not None:
            shutil.rmtree(self._http_archive_dir, ignore_errors=True)
            self._http_archive_dir = None
            self._http_archive_path = None


def browser() -> BrowserSession:
    """Return an async context manager owning an interactive capture session.

    The session owns the Playwright driver, the remote browser connection, an
    isolated browser context recording a HAR, and one real page (``.page``).
    The caller drives the page directly through Playwright; call
    ``.snapshot()`` (terminal) to capture and persist the current state.

    All configuration comes from the environment (``BROWSER_WS_URL``,
    ``DATABASE_URL``, ``STORAGE_BASE_PATH``); there are no overrides.
    """
    return BrowserSession()
