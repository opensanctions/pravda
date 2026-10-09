"""Configured browser capture and snapshot history."""

import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, async_playwright
from playwright.async_api import Error as PlaywrightError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pravda.capture import (
    CaptureResult,
    DriveCallback,
    DriveTimeoutError,
    capture_driven,
    capture_page,
    is_http_url,
)
from pravda.config import PravdaConfig, Timeouts
from pravda.db import SnapshotRecord
from pravda.http_archive import capture_http_archive
from pravda.snapshots import Snapshot, from_record
from pravda.standalone import capture_standalone_html
from pravda.storage import Storage

logger = logging.getLogger(__name__)

# Capture and finalization form one atomic evidence operation. A context-close
# failure invalidates the result; cleanup is best-effort. Storage, persistence,
# non-Playwright callback failures, and cancellation propagate. Standalone-HTML
# derivation failures drop only that artifact.


class _CaptureFailure(Exception):
    """A browser failure that should be persisted without evidence."""


@dataclass
class StoredArtifacts:
    """Stored artifact names and manifest produced by finalization."""

    rendered_html: str | None = None
    screenshot: str | None = None
    standalone_html: str | None = None
    http_archive: dict | None = None


async def _connect(playwright, browser_ws_url: str, timeouts: Timeouts) -> Browser:
    """Connect to the remote browser server."""
    return await playwright.chromium.connect(
        browser_ws_url,
        # connect's timeout defaults to 0 (no timeout). Bound the handshake so
        # a dead server fails fast as a PlaywrightError instead of hanging.
        timeout=timeouts.connect_milliseconds,
    )


async def _finalize_capture(
    context: BrowserContext,
    result: CaptureResult,
    http_archive_path: Path,
    storage: Storage,
    timeouts: Timeouts,
) -> StoredArtifacts:
    """Close the context and store the captured evidence atomically."""
    try:
        async with asyncio.timeout(timeouts.context_close_milliseconds / 1000):
            await context.close()
    except asyncio.TimeoutError as error:
        message = (
            f"browser context close exceeded "
            f"{timeouts.context_close_milliseconds:g}ms budget"
        )
        raise _CaptureFailure(message) from error
    except Exception as error:
        raise _CaptureFailure(f"browser context close failed: {error}") from error

    if result.http_status is None:
        return StoredArtifacts()
    if not http_archive_path.exists():
        raise _CaptureFailure(
            "browser context closed without producing an HTTP archive"
        )

    rendered_html = None
    if result.html is not None:
        rendered_html = await storage.put_artifact(
            result.html.encode(), "html", result.final_url
        )
    screenshot = None
    if result.screenshot is not None:
        screenshot = await storage.put_artifact(
            result.screenshot, "png", result.final_url
        )

    async with asyncio.timeout(timeouts.har_processing_milliseconds / 1000):
        http_archive = await capture_http_archive(
            http_archive_path, result.final_url, storage, download=result.download
        )
    if http_archive is None:
        raise _CaptureFailure("browser context produced an invalid HTTP archive")

    standalone_html = None
    if result.html is not None:
        try:
            async with asyncio.timeout(timeouts.standalone_milliseconds / 1000):
                standalone_html = await capture_standalone_html(
                    http_archive_path,
                    http_archive,
                    result.html,
                    result.final_url,
                    storage,
                )
        except Exception as error:
            logger.warning(
                "Standalone HTML derivation failed for %s: %s",
                result.final_url,
                error,
            )
    return StoredArtifacts(
        rendered_html=rendered_html,
        screenshot=screenshot,
        standalone_html=standalone_html,
        http_archive=http_archive,
    )


async def _cleanup(browser: Browser | None, playwright, timeouts: Timeouts) -> None:
    """Best-effort cleanup that never changes the capture outcome."""
    operations = []
    if browser is not None:
        operations.append(("browser.close", browser.close))
    if playwright is not None:
        operations.append(("playwright.stop", playwright.stop))

    for name, operation in operations:
        try:
            async with asyncio.timeout(timeouts.cleanup_milliseconds / 1000):
                await operation()
        except asyncio.TimeoutError:
            logger.warning(
                "%s exceeded %gms budget", name, timeouts.cleanup_milliseconds
            )
        except Exception as error:
            logger.warning("%s failed: %s", name, error)


async def _persist_snapshot(
    url: str,
    result: CaptureResult,
    artifacts: StoredArtifacts,
    sessionmaker: async_sessionmaker,
    storage: Storage,
    timeouts: Timeouts,
) -> Snapshot:
    """Commit captured evidence and return its public value."""
    record = SnapshotRecord(
        url=url,
        final_url=result.final_url,
        http_status=result.http_status,
        error=result.error,
        rendered_html=artifacts.rendered_html,
        screenshot=artifacts.screenshot,
        standalone_html=artifacts.standalone_html,
        http_archive=artifacts.http_archive,
    )
    async with asyncio.timeout(timeouts.persist_milliseconds / 1000):
        async with sessionmaker() as session:
            session.add(record)
            await session.commit()
    logger.info(
        "Captured %s id=%s: status=%s http_archive=%s error=%s",
        record.url,
        record.id,
        record.http_status,
        record.http_archive is not None,
        record.error,
    )
    return from_record(record, storage)


async def _capture_to_result(
    url: str,
    *,
    drive: DriveCallback | None,
    browser_ws_url: str,
    http_archive_dir: Path,
    storage: Storage,
    timeouts: Timeouts,
) -> tuple[CaptureResult, StoredArtifacts]:
    """Capture and finalize one atomic evidence bundle."""
    playwright = None
    browser: Browser | None = None
    deadline = asyncio.timeout(timeouts.snapshot_milliseconds / 1000)
    try:
        try:
            async with deadline:
                playwright = await async_playwright().start()
                browser = await _connect(playwright, browser_ws_url, timeouts)

                http_archive_path = http_archive_dir / "record.zip"
                context = await browser.new_context(
                    record_har_path=str(http_archive_path),
                    record_har_content="attach",
                )
                page = await context.new_page()

                if drive is None:
                    result = await capture_page(page, url, timeouts)
                else:
                    result = await capture_driven(page, url, drive, timeouts)

                artifacts = await _finalize_capture(
                    context, result, http_archive_path, storage, timeouts
                )
                return result, artifacts
        except asyncio.TimeoutError:
            # Only the outer breaker is a persisted browser failure. Inner
            # storage and HAR-processing timeouts remain operational errors.
            if not deadline.expired():
                raise
            raise _CaptureFailure(
                f"snapshot exceeded {timeouts.snapshot_milliseconds:g}ms"
                " wall-clock budget"
            )
    finally:
        await _cleanup(browser, playwright, timeouts)


async def _capture(
    url: str,
    *,
    drive: DriveCallback | None,
    browser_ws_url: str,
    sessionmaker: async_sessionmaker,
    storage: Storage,
    timeouts: Timeouts,
) -> Snapshot:
    """Capture *url* and persist the result."""
    logger.info("Capturing %s", url)

    http_archive_dir = Path(tempfile.mkdtemp())
    try:
        try:
            result, artifacts = await _capture_to_result(
                url,
                drive=drive,
                browser_ws_url=browser_ws_url,
                http_archive_dir=http_archive_dir,
                storage=storage,
                timeouts=timeouts,
            )
        except (DriveTimeoutError, PlaywrightError, _CaptureFailure) as error:
            logger.error("Capture failed for %s: %s", url, error)
            result = CaptureResult(error=str(error))
            artifacts = StoredArtifacts()
    finally:
        shutil.rmtree(http_archive_dir, ignore_errors=True)

    return await _persist_snapshot(
        url, result, artifacts, sessionmaker, storage, timeouts
    )


class Pravda:
    """Configured async entry point for capture and snapshot history.

    The application owns the database engine and its ``async_sessionmaker``,
    which must be configured with ``expire_on_commit=False``. Each concurrent
    capture owns its browser connection, recording context, temporary
    directory, and database session.
    """

    def __init__(
        self,
        config: PravdaConfig,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._sessionmaker = sessionmaker
        self._timeouts = config.timeouts
        self._storage = Storage.from_url(config.storage_base_path)
        self._browser_ws_url = config.browser_ws_url

    async def snapshot(
        self, url: str, *, drive: DriveCallback | None = None
    ) -> Snapshot:
        """Capture and persist an HTTP(S) URL.

        Without *drive*, Pravda navigates and waits for normal page load. With
        *drive*, the callback owns navigation and interaction on the supplied
        recording page; Pravda then captures its current state. The first
        download triggered by the callback becomes the capture subject.

        The capture and HAR finalization have one wall-clock bound. Playwright
        and context-close failures are persisted without artifacts; storage,
        non-Playwright callback, and persistence errors propagate. Cleanup is
        best-effort.
        """
        if not is_http_url(url):
            raise ValueError(f"snapshot URL must be http(s), got {url!r}")
        return await _capture(
            url,
            drive=drive,
            browser_ws_url=self._browser_ws_url,
            sessionmaker=self._sessionmaker,
            storage=self._storage,
            timeouts=self._timeouts,
        )

    async def snapshots(self, url: str) -> list[Snapshot]:
        """Return all exact-URL matches, newest first."""
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(SnapshotRecord)
                .where(SnapshotRecord.url == url)
                .order_by(SnapshotRecord.captured_at.desc())
            )
            rows = result.scalars().all()
            return [from_record(row, self._storage) for row in rows]
