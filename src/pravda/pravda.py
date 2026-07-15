"""The configured Pravda entry point: :class:`Pravda` and :class:`PravdaConfig`.

This module owns the browser capture orchestration (connect, set up a
recording context, capture, finalize, persist) and the history query, exposed
as async methods on a long-lived, explicitly configured :class:`Pravda`
instance. The page-level evidence extraction (navigate, wait,
screenshot/HTML/text, download recovery) lives in :mod:`pravda.capture`; the
HAR unpacking in :mod:`pravda.http_archive`. Here we wire those pieces into
the public surface:

* :meth:`Pravda.snapshot` — connect, set up a recording context, capture,
  finalize, persist, all under a wall-clock breaker so a wedged stage becomes
  a bounded, persisted failure. Without ``drive`` it uses the default
  behavior (navigate to the normal ``load`` state); with ``drive`` the caller
  pilots the recording page itself — owning the initial navigation and any
  readiness or interaction it needs — and Pravda captures whatever state it
  leaves behind.
* :meth:`Pravda.snapshots` — every exact-URL match, newest first.

There is no readiness-condition abstraction. The default (no ``drive``) is a
fixed internal readiness (navigate to commit, then wait for the normal
``load`` state); callers needing anything else pass a ``drive`` callback and
use Playwright directly on the recording page. Pravda owns its database
sessions: every attempt (success, partial, or failure) is committed through
its own session factory and returned as a public
:class:`~pravda.snapshots.Snapshot`. A :class:`PravdaConfig` supplies the
runtime settings for each instance.
"""

import asyncio
import json
import logging
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    Page,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pravda.capture import CaptureResult, capture_current, capture_page
from pravda.db import SnapshotRecord
from pravda.http_archive import capture_http_archive
from pravda.snapshots import Snapshot, from_record
from pravda.storage import Storage

logger = logging.getLogger(__name__)

BROWSER_CHANNEL = "chrome"

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


@dataclass(frozen=True)
class PravdaConfig:
    """Explicit, typed configuration for a :class:`Pravda` instance.

    Attributes:
        database_url: async SQLAlchemy Postgres URL (e.g.
            ``postgresql+asyncpg://user:pass@host/db``).
        browser_ws_url: remote Playwright WebSocket URL.
        storage_base_path: fsspec storage URL, such as ``./data``,
            ``s3://bucket``, or ``gs://bucket``.
    """

    database_url: str
    browser_ws_url: str
    storage_base_path: str


def _launch_options_header() -> dict[str, str]:
    """Encode the Chrome launch options for the WebSocket server header."""
    return {
        "x-playwright-launch-options": json.dumps(
            {"channel": BROWSER_CHANNEL, "headless": False}
        ),
    }


async def _connect(playwright, browser_ws_url: str) -> Browser:
    """Connect to the remote browser server under a bounded handshake."""
    return await playwright.chromium.connect(
        browser_ws_url,
        # connect's timeout defaults to 0 (no timeout). Bound the handshake so
        # a dead server fails fast as a PlaywrightError instead of waiting on
        # the wall-clock budget.
        timeout=CONNECT_TIMEOUT_MS,
        headers=_launch_options_header(),
    )


def _compose_error(existing: str | None, addition: str) -> str:
    """Combine an existing capture error with a later finalization error.

    A finalization failure (context close, HAR processing) is appended to any
    error the capture already recorded (e.g. a load timeout) rather than
    overwriting it, so both the page-level and finalization-level causes stay
    visible on the persisted snapshot.
    """
    if existing:
        return f"{existing}; {addition}"
    return addition


async def _finalize_capture(
    context: BrowserContext,
    result: CaptureResult,
    http_archive_path: Path | None,
    url: str,
    stages: dict[str, float],
    storage: Storage,
) -> tuple[CaptureResult, dict | None]:
    """Close the context (flushing the HAR) and unpack the archive.

    ``context.close()`` has no Playwright timeout, so bound it. If it wedges or
    fails operationally, the HAR it would flush is unusable and is discarded —
    but the page evidence ``capture_page``/``capture_current`` already returned
    is kept, with the failure composed onto any error the capture already
    recorded so the snapshot is not mistaken for a success. HAR processing
    (unzipping, parsing the manifest, storing every body) follows the same
    policy on its own timeout or operational failure: the manifest is treated
    as an atomic stage and discarded whole, so a manifest is never returned
    whose ``_file`` points at a body that failed to store.

    *url* is the page URL (for logging). Returns the (possibly error-amended)
    result and the HAR manifest, or ``None`` when navigation did not commit,
    no archive was recorded, or the archive was discarded. *stages* records
    the ``close`` and ``har`` durations for the completion log.
    """
    # Close the recording context, flushing the HAR zip. A timeout or
    # operational failure leaves the HAR unusable: discard it, keep the page
    # evidence, and record the cause alongside any prior capture error.
    stage_start = time.monotonic()
    close_error: str | None = None
    try:
        async with asyncio.timeout(CONTEXT_CLOSE_TIMEOUT_S):
            await context.close()
    except asyncio.TimeoutError:
        close_error = (
            f"browser context close exceeded {CONTEXT_CLOSE_TIMEOUT_S}s "
            f"budget; http archive discarded"
        )
    except Exception as exception:
        close_error = (
            f"browser context close failed ({exception}); http archive discarded"
        )
    stages["close"] = time.monotonic() - stage_start
    if close_error is not None:
        logger.error("%s for %s", close_error, url)
        return (
            replace(result, error=_compose_error(result.error, close_error)),
            None,
        )

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

    # Unpack the archive and store every body as one bounded, atomic stage. A
    # timeout or operational failure (parse error, storage error) discards the
    # manifest whole — never a manifest with a dangling body reference — while
    # the page evidence and any prior capture error are preserved.
    http_archive: dict | None = None
    har_error: str | None = None
    stage_start = time.monotonic()
    try:
        async with asyncio.timeout(HAR_PROCESSING_TIMEOUT_S):
            http_archive = await capture_http_archive(
                http_archive_path, result.final_url, storage, download=result.download
            )
    except asyncio.TimeoutError:
        har_error = (
            f"HAR processing exceeded {HAR_PROCESSING_TIMEOUT_S}s "
            f"budget; http archive discarded"
        )
    except Exception as exception:
        har_error = f"HAR processing failed ({exception}); http archive discarded"
    stages["har"] = time.monotonic() - stage_start
    if har_error is not None:
        logger.error("%s for %s", har_error, url)
        return (
            replace(result, error=_compose_error(result.error, har_error)),
            None,
        )
    return result, http_archive


async def _close_browser(browser: Browser) -> None:
    """Force-close a connected browser under a bounded timeout.

    ``browser.close()`` clears contexts and disconnects from the remote server
    (analogous to force-quitting); it has no timeout argument, so bound it.
    Cleanup runs after the snapshot is finalized, so any failure here —
    timeout or operational — is logged as an operational warning only and never
    propagates, so it cannot prevent the finalized snapshot from persisting.
    """
    try:
        async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT_S):
            await browser.close()
    except asyncio.TimeoutError:
        logger.warning(
            "browser.close exceeded %ss budget; cleanup abandoned",
            BROWSER_CLOSE_TIMEOUT_S,
        )
    except Exception as error:
        logger.warning("browser.close failed: %s", error)


async def _persist_snapshot(
    url: str,
    result: CaptureResult,
    http_archive: dict | None,
    stages: dict[str, float],
    sessionmaker: async_sessionmaker,
    storage: Storage,
) -> Snapshot:
    """Map captured evidence onto a ``SnapshotRecord`` row and commit it.

    Pravda owns its database session: the row is added and committed through
    *sessionmaker* (not a caller-supplied session), so a committed attempt is
    immediately visible to every reader of the shared database — including
    :meth:`Pravda.snapshots`. Returns the public
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
    async with sessionmaker() as session:
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
    return from_record(record, storage)


# An async callback that pilots a freshly created recording page: it owns
# every navigation and interaction (selectors, load states, clicks, form
# fills), using Playwright directly. Passed to :meth:`Pravda.snapshot` as
# ``drive``.
DriveCallback = Callable[[Page, str], Awaitable[None]]


async def _drive_capture(
    page: Page, url: str, drive: DriveCallback, storage: Storage
) -> CaptureResult:
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
    handling in :func:`_capture` (persisted as a failed attempt); any other
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
        return await capture_current(
            page, final_url, navigation_status, download, storage
        )
    finally:
        page.remove_listener("download", on_download)
        page.remove_listener("response", on_response)


async def _capture(
    url: str,
    *,
    drive: DriveCallback | None,
    browser_ws_url: str,
    sessionmaker: async_sessionmaker,
    storage: Storage,
) -> Snapshot:
    """Run the full capture pipeline for *url* and persist the result.

    This is the body of :meth:`Pravda.snapshot`, factored out with its
    dependencies (browser URL, session factory, storage) passed explicitly.
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
                    browser = await _connect(playwright, browser_ws_url)
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
                        result = await capture_page(page, url, storage)
                    else:
                        result = await _drive_capture(page, url, drive, storage)
                    stages["capture"] = time.monotonic() - stage_start

                    # Close the context (flushing the HAR) and unpack the
                    # archive. Both carry fatal-evidence semantics on a timeout
                    # or operational failure: the page evidence already
                    # captured is kept, the failure is composed onto any prior
                    # capture error, and the (potentially incomplete) HAR is
                    # discarded.
                    result, http_archive = await _finalize_capture(
                        context,
                        result,
                        http_archive_path,
                        url,
                        stages,
                        storage,
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
        # A Playwright error escaped the inner stages — connect, context/page
        # setup, or a drive callback's Playwright error (capture_page and
        # capture_current absorb their own). No page evidence had been
        # captured yet at this point, so record an empty, failed result.
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

    return await _persist_snapshot(
        url, result, http_archive, stages, sessionmaker, storage
    )


class Pravda:
    """Configured, long-lived entry point for evidence capture.

    Owns an async SQLAlchemy engine/session factory and an fsspec storage
    backend, built from a :class:`PravdaConfig`. Use it as an async context
    manager so the engine is disposed on teardown:

        config = PravdaConfig(
            database_url=...,
            browser_ws_url=...,
            storage_base_path=...,
        )
        async with Pravda(config) as pravda:
            snapshot = await pravda.snapshot(url)
            history = await pravda.snapshots(url)

    Browser connections are opened per capture (the browser itself is not a
    long-lived resource here), so concurrent ``snapshot`` calls are safe: each
    owns its own browser connection, recording context, temporary directory,
    and database session, all sharing only the pooled engine and the storage
    backend.
    """

    def __init__(self, config: PravdaConfig) -> None:
        self._config = config
        self._engine = create_async_engine(config.database_url)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        self._storage = Storage.from_url(config.storage_base_path)
        self._browser_ws_url = config.browser_ws_url

    async def __aenter__(self) -> "Pravda":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Dispose the owned database engine. Call once when finished.

        Runs automatically on ``async with`` teardown. Storage holds its
        filesystem configuration, while browser connections close after each
        capture.
        """
        await self._engine.dispose()

    async def snapshot(
        self, url: str, *, drive: DriveCallback | None = None
    ) -> Snapshot:
        """Capture a snapshot of *url* and persist it.

        Without *drive* this is the default behavior: connect to the remote
        browser, set up an isolated context recording a HAR, navigate (waiting
        for the normal ``load`` state), capture the page evidence, flush and
        process the HAR, then commit the result.

        With *drive*, Pravda still owns the browser connection, the recording
        context, the page, and persistence — but the caller drives the page.
        Pravda creates the recording page, hands it (and *url*) to
        ``await drive(page, url)``, then captures the resulting current page
        state. The callback has complete control, including the initial
        navigation and any readiness or interaction it needs (selectors, load
        states, clicks, form fills); use Playwright directly on *page*. A
        navigation that hands off to a download (e.g. a PDF) is recovered too:
        ``goto`` raises ``Download is starting``, so catch it inside *drive*
        and Pravda observes the download event and captures the bytes, as the
        default path does internally.

        The whole pipeline runs under a wall-clock breaker so a wedged stage
        becomes a bounded failure. Browser/navigation/timeout failures —
        including Playwright errors and timeouts raised from *drive* — are
        still persisted as :class:`~pravda.snapshots.Snapshot` rows (with
        ``error`` set and no evidence); a database failure that prevents the
        commit propagates. A failure while finalizing the capture (closing the
        recording context or processing the HAR) does not discard page evidence
        already captured: the snapshot is persisted with the page artifacts,
        an ``error`` describing the finalization failure composed onto any
        earlier capture error, and no HAR. An arbitrary (non-Playwright,
        non-timeout) exception raised by *drive* is not turned into a snapshot
        failure: Pravda cleans up and re-raises it to the caller.

        The recorded ``url`` is *url* (the subject the caller asked to
        capture); ``final_url`` reflects where the page actually landed
        (``page.url``, or the download URL when a download was captured).
        Returns the public :class:`~pravda.snapshots.Snapshot` for the attempt.
        """
        return await _capture(
            url,
            drive=drive,
            browser_ws_url=self._browser_ws_url,
            sessionmaker=self._sessionmaker,
            storage=self._storage,
        )

    async def snapshots(self, url: str) -> list[Snapshot]:
        """Return every snapshot captured for *url*, newest first.

        Exact-URL match only (no normalization). Uses Pravda's own session
        factory, so callers need no database wiring. Returns public
        :class:`~pravda.snapshots.Snapshot` values; there is no pagination —
        every match is returned.
        """
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(SnapshotRecord)
                .where(SnapshotRecord.url == url)
                .order_by(SnapshotRecord.captured_at.desc())
            )
            rows = result.scalars().all()
            return [from_record(row, self._storage) for row in rows]
