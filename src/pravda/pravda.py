"""The configured Pravda entry point: :class:`Pravda` and :class:`PravdaConfig`.

This module owns the browser capture orchestration (start Playwright, connect,
set up a recording context, capture, finalize, clean up, shut down, persist)
and the history query, exposed as async methods on a long-lived, explicitly
configured :class:`Pravda` instance. The page-level evidence extraction
(navigate, wait, screenshot/HTML/text, download recovery, and driven
capture) lives in :mod:`pravda.capture`; the HAR unpacking in
:mod:`pravda.http_archive`. Here we
wire those pieces into the public surface:

* :meth:`Pravda.snapshot` — the full capture pipeline, bounded phase by phase
  (no single outer breaker): start the Playwright driver, connect, set up a
  recording context, capture (or run a ``drive`` callback), finalize, clean up
  the browser, shut the driver down, and commit. Without ``drive`` it uses the
  default behavior (navigate to the normal ``load`` state); with ``drive`` the
  caller pilots the recording page itself — owning the initial navigation and
  any readiness or interaction it needs — and Pravda captures whatever state it
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
from dataclasses import dataclass, replace
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pravda.capture import (
    DRIVE_TIMEOUT_S,
    CaptureResult,
    DriveCallback,
    capture_driven,
    capture_page,
)
from pravda.db import SnapshotRecord
from pravda.http_archive import capture_http_archive
from pravda.snapshots import Snapshot, from_record
from pravda.storage import Storage

logger = logging.getLogger(__name__)

BROWSER_CHANNEL = "chrome"

# --- Snapshot deadline architecture --------------------------------------
#
# snapshot() is bounded phase by phase, not by a single outer breaker. Each
# phase below carries its own wall-clock budget; the phases are independent,
# so a slow stage never erases evidence an earlier stage already captured
# (the failure mode of one breaker whose budget is smaller than the legitimate
# sum of the inner budgets). The documented worst case is simply the sum of
# the phase budgets — there is no shorter outer deadline to trip:
#
#   startup   PLAYWRIGHT_START_TIMEOUT_S             10
#   connect   CONNECT_TIMEOUT_MS / 1000              10
#   setup     SETUP_TIMEOUT_S                        10
#   capture   default: capture_page's inner budgets
#                  (nav 10 + load 30 + DOM 10 + shot 15
#                   + 3 storage writes 45 = 110 worst case, no download)
#             drive:    DRIVE_TIMEOUT_S (60) + capture_current's inner budgets
#   finalize  CONTEXT_CLOSE_TIMEOUT_S
#           + HAR_PROCESSING_TIMEOUT_S               10 + 20
#   cleanup   BROWSER_CLOSE_TIMEOUT_S                 5   (best-effort)
#   shutdown  PLAYWRIGHT_STOP_TIMEOUT_S               5   (best-effort)
#   persist   PERSIST_TIMEOUT_S                      15   (propagates on failure)
#
# A capture result, once produced, is preserved through finalize and cleanup:
# a failure there composes an error but keeps the page evidence (and discards
# the HAR when that stage failed). Only persistence can drop a finalized
# result, and a persistence timeout/error propagates as a database failure
# rather than being pretended persisted. Browser/navigation/drive Playwright
# and capture-phase timeouts become persisted failed snapshots (empty result);
# an arbitrary non-Playwright exception from a drive callback propagates after
# cleanup and persists nothing. Cancellation (BaseException) is never caught.

# Playwright driver startup (async_playwright().start()): launches the local
# driver subprocess. Bound it explicitly so a wedged startup fails fast; we
# call start()/stop() directly (not ``async with``) so both ends are bounded
# instead of relying on an unbounded context-manager teardown.
PLAYWRIGHT_START_TIMEOUT_S = 10

# Connect timeout (ms), passed to playwright.chromium.connect (its default is
# 0 / no timeout). A dead server fails fast as a PlaywrightError.
CONNECT_TIMEOUT_MS = 10_000

# Combined budget for new_context() + new_page(). Both reach the remote server
# over a pipe; a timeout here is a capture-phase failure (no evidence yet).
SETUP_TIMEOUT_S = 10

# Finalization budgets. context.close() (which flushes the HAR zip) has no
# Playwright timeout argument; bounding it preserves already-captured page
# evidence and discards an incomplete HAR. HAR processing (unzip + store every
# body) is one atomic stage on its own budget.
CONTEXT_CLOSE_TIMEOUT_S = 10
HAR_PROCESSING_TIMEOUT_S = 20

# Best-effort cleanup budgets. Both run after the snapshot is finalized, so a
# timeout or operational failure is an operational warning only and never
# alters the finalized state.
BROWSER_CLOSE_TIMEOUT_S = 5
PLAYWRIGHT_STOP_TIMEOUT_S = 5

# Persistence budget. A wedged commit is a database failure: bound it, and let
# the timeout/error propagate (nothing is persisted; we never pretend it was).
PERSIST_TIMEOUT_S = 15


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


def _empty_result(error: str | None = None) -> CaptureResult:
    """An evidence-less capture result, optionally carrying an *error*.

    The default outcome of a pre-capture failure (startup/connect/setup/drive):
    no page ever produced evidence, so the persisted snapshot carries only the
    failure cause.
    """
    return CaptureResult(
        http_status=None,
        error=error,
        final_url=None,
        plaintext=None,
        rendered_html=None,
        screenshot=None,
        download=None,
    )


async def _start_playwright(stages: dict[str, float]):
    """Start the Playwright driver under :data:`PLAYWRIGHT_START_TIMEOUT_S`.

    ``async_playwright().start()`` launches the local driver subprocess. We
    call ``start``/``stop`` directly (rather than ``async with``) so both ends
    of the driver lifecycle are bounded explicitly instead of relying on the
    context manager's unbounded teardown outside any deadline.
    """
    stage_start = time.monotonic()
    async with asyncio.timeout(PLAYWRIGHT_START_TIMEOUT_S):
        playwright = await async_playwright().start()
    stages["startup"] = time.monotonic() - stage_start
    return playwright


async def _stop_playwright(playwright, stages: dict[str, float]) -> None:
    """Stop the Playwright driver under :data:`PLAYWRIGHT_STOP_TIMEOUT_S`.

    Mirrors the browser-cleanup policy: it runs after the snapshot is
    finalized, so a timeout or operational failure is an operational warning
    only — it never alters the finalized state and never propagates.
    """
    stage_start = time.monotonic()
    try:
        async with asyncio.timeout(PLAYWRIGHT_STOP_TIMEOUT_S):
            await playwright.stop()
    except asyncio.TimeoutError:
        logger.warning(
            "playwright.stop exceeded %ss budget; driver left running",
            PLAYWRIGHT_STOP_TIMEOUT_S,
        )
    except Exception as error:  # best-effort: never alter finalized state
        logger.warning("playwright.stop failed: %s", error)
    stages["shutdown"] = time.monotonic() - stage_start


async def _connect(
    playwright, browser_ws_url: str, stages: dict[str, float]
) -> Browser:
    """Connect to the remote browser server under a bounded handshake."""
    stage_start = time.monotonic()
    browser = await playwright.chromium.connect(
        browser_ws_url,
        # connect's timeout defaults to 0 (no timeout). Bound the handshake so
        # a dead server fails fast as a PlaywrightError instead of hanging.
        timeout=CONNECT_TIMEOUT_MS,
        headers=_launch_options_header(),
    )
    stages["connect"] = time.monotonic() - stage_start
    return browser


async def _setup(
    browser: Browser, http_archive_dir: Path, stages: dict[str, float]
) -> tuple[BrowserContext, Page, Path]:
    """Create the HAR-recording context and page under :data:`SETUP_TIMEOUT_S`.

    Both calls reach the remote server over a pipe; bounding them catches a
    wedge before a page exists. The HAR zip is written to *http_archive_dir*
    and flushed when the context closes. Raises ``asyncio.TimeoutError`` on
    timeout; the caller describes the failure (no evidence existed yet).
    """
    http_archive_path = http_archive_dir / "record.zip"
    stage_start = time.monotonic()
    async with asyncio.timeout(SETUP_TIMEOUT_S):
        context = await browser.new_context(
            record_har_path=str(http_archive_path),
            record_har_content="attach",
        )
        page = await context.new_page()
    stages["setup"] = time.monotonic() - stage_start
    return context, page, http_archive_path


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
    propagates, so it cannot alter the finalized snapshot state.
    """
    try:
        async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT_S):
            await browser.close()
    except asyncio.TimeoutError:
        logger.warning(
            "browser.close exceeded %ss budget; cleanup abandoned",
            BROWSER_CLOSE_TIMEOUT_S,
        )
    except Exception as error:  # best-effort: never alter finalized state
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
    :class:`~pravda.snapshots.Snapshot` for the committed row. The commit is
    bounded by :data:`PERSIST_TIMEOUT_S`; a timeout or database error
    propagates to the caller — a wedged commit is a database failure, and we
    never pretend a row was persisted when it was not.
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
    async with asyncio.timeout(PERSIST_TIMEOUT_S):
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


async def _capture_to_result(
    url: str,
    *,
    drive: DriveCallback | None,
    browser_ws_url: str,
    stages: dict[str, float],
    http_archive_dir: Path,
    storage: Storage,
) -> tuple[CaptureResult, dict | None]:
    """Run the capture pipeline (startup through driver shutdown) and return
    the finalized ``(result, http_archive)``, or raise.

    The phases run under their own budgets (see the deadline-architecture
    note atop this module); there is no single outer breaker, so a slow stage
    never erases evidence an earlier stage already captured:

    * startup/connect/setup/drive failures (Playwright error or timeout) — no
      evidence existed yet — yield an empty failed result;
    * a successful capture is finalized with fatal-evidence semantics that
      preserve the page evidence on a close/HAR failure (composing the cause
      onto any capture error and discarding the HAR);
    * an arbitrary (non-Playwright, non-timeout) exception from a ``drive``
      callback propagates after cleanup, persisting nothing.

    Cleanup (browser.close, playwright.stop) always runs once the driver
    started: it is best-effort and bounded, and never alters the finalized
    state. Cancellation (BaseException) is never caught.
    """
    # Phase 1: start the Playwright driver (bounded). A wedge here is a
    # pre-capture infra failure: nothing to clean up and no evidence, so
    # record an empty failed result for the caller to persist.
    try:
        playwright = await _start_playwright(stages)
    except Exception as error:
        logger.error("Playwright startup failed for %s: %s", url, error)
        return _empty_result(f"playwright startup failed: {error}"), None

    result = _empty_result()
    http_archive: dict | None = None
    browser: Browser | None = None
    try:
        # Names the stage whose budget tripped when an asyncio.TimeoutError
        # escapes below (setup or drive); set immediately before each bounded
        # call that can raise one.
        timeout_message: str | None = None
        try:
            # Phase 2: connect (bounded handshake; a timeout raises a
            # PlaywrightError, not asyncio.TimeoutError).
            browser = await _connect(playwright, browser_ws_url, stages)
            # Phase 3: set up the recording context and page (bounded).
            timeout_message = f"context/page setup exceeded {SETUP_TIMEOUT_S}s budget"
            context, page, http_archive_path = await _setup(
                browser, http_archive_dir, stages
            )
            # Phase 4: capture. The default path is bounded internally by
            # capture_page (it absorbs its own failures into the result); a
            # drive callback is bounded by DRIVE_TIMEOUT_S.
            capture_start = time.monotonic()
            if drive is None:
                result = await capture_page(page, url, storage)
            else:
                timeout_message = f"drive callback exceeded {DRIVE_TIMEOUT_S}s budget"
                result = await capture_driven(page, url, drive, storage)
            stages["capture"] = time.monotonic() - capture_start
        except PlaywrightError as error:
            # connect/setup/drive raised before evidence existed (capture_page
            # and capture_current absorb their own failures into the result).
            logger.error("Browser error for %s: %s", url, error.message)
            result = _empty_result(error.message)
        except asyncio.TimeoutError:
            # setup or drive exceeded its budget. No partial evidence is
            # preserved here — the dedicated context.close/HAR handlers in
            # _finalize_capture own the partial-evidence policy for those.
            message = timeout_message or "capture exceeded its budget"
            logger.error("%s for %s", message, url)
            result = _empty_result(message)
        else:
            # Phase 5: finalize — close the context (flush HAR) and unpack the
            # archive. Preserves already-captured evidence on its own timeout
            # or operational failure.
            result, http_archive = await _finalize_capture(
                context,
                result,
                http_archive_path,
                url,
                stages,
                storage,
            )
    finally:
        # Phase 6: best-effort, bounded browser cleanup. Runs on every path
        # once a browser connected; never alters the finalized state.
        if browser is not None:
            await _close_browser(browser)
        # Phase 7: best-effort, bounded Playwright shutdown.
        await _stop_playwright(playwright, stages)

    return result, http_archive


async def _capture(
    url: str,
    *,
    drive: DriveCallback | None,
    browser_ws_url: str,
    sessionmaker: async_sessionmaker,
    storage: Storage,
) -> Snapshot:
    """Run the full capture pipeline for *url* and persist the result.

    Produces the finalized evidence (or propagates an arbitrary drive-callback
    exception after cleanup, persisting nothing), then commits it. This is the
    body of :meth:`Pravda.snapshot`, factored out with its dependencies
    (browser URL, session factory, storage) passed explicitly.
    """
    logger.info("Capturing %s", url)

    stages: dict[str, float] = {}
    http_archive_dir = Path(tempfile.mkdtemp())
    try:
        result, http_archive = await _capture_to_result(
            url,
            drive=drive,
            browser_ws_url=browser_ws_url,
            stages=stages,
            http_archive_dir=http_archive_dir,
            storage=storage,
        )
    finally:
        # The temp HAR dir is process-local and trivial to remove; it lives
        # only as long as capture + finalize need it.
        shutil.rmtree(http_archive_dir, ignore_errors=True)

    # Phase 8: persist (bounded; propagates on failure). Reached on success
    # and on a handled pre-capture/finalize failure; NOT reached when an
    # arbitrary drive-callback exception propagated above.
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

        Without *drive* this is the default behavior: start Playwright,
        connect to the remote browser, set up an isolated context recording a
        HAR, navigate (waiting for the normal ``load`` state), capture the
        page evidence, flush and process the HAR, then commit the result.

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

        The whole pipeline is bounded phase by phase (driver startup,
        connect, setup, capture or drive callback, finalization, browser
        cleanup, driver shutdown, and the database commit) — there is no
        single outer breaker, so a slow stage cannot erase evidence an earlier
        stage already captured. Browser/navigation/drive Playwright and
        capture-phase timeouts become persisted failed snapshots (an empty
        result with ``error`` set); a drive callback is bounded, and a
        Playwright error or timeout it raises is likewise persisted as a
        failed attempt. A failure while finalizing the capture (closing the
        recording context or processing the HAR) does not discard page
        evidence already captured: the snapshot is persisted with the page
        artifacts, an ``error`` describing the finalization failure composed
        onto any earlier capture error, and no HAR. A wedged database commit
        is a database failure: it propagates (the attempt is not pretended to
        be persisted). Browser cleanup and driver shutdown are best-effort and
        bounded, and never change the finalized state. An arbitrary
        (non-Playwright, non-timeout) exception raised by *drive* is not
        turned into a snapshot failure: Pravda cleans up and re-raises it to
        the caller.

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
