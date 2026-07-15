"""Configured browser capture and snapshot history."""

import asyncio
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, async_playwright
from playwright.async_api import Error as PlaywrightError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pravda.capture import (
    CaptureResult,
    DriveCallback,
    DriveTimeoutError,
    capture_driven,
    capture_page,
    is_http_url,
)
from pravda.db import SnapshotRecord
from pravda.http_archive import capture_http_archive
from pravda.snapshots import Snapshot, from_record
from pravda.storage import Storage

logger = logging.getLogger(__name__)

BROWSER_CHANNEL = "chrome"

# Capture and finalization form one atomic evidence operation. A context-close
# failure invalidates the result; cleanup is best-effort. Storage, persistence,
# non-Playwright callback failures, and cancellation propagate.
SNAPSHOT_TIMEOUT_S = 240
CONNECT_TIMEOUT_MS = 10_000
CONTEXT_CLOSE_TIMEOUT_S = 30
HAR_PROCESSING_TIMEOUT_S = 20
CLEANUP_TIMEOUT_S = 5
PERSIST_TIMEOUT_S = 15


@dataclass(frozen=True)
class PravdaConfig:
    """Runtime configuration for a :class:`Pravda` instance."""

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
    """Return an evidence-less capture result."""
    return CaptureResult(
        http_status=None,
        error=error,
        final_url=None,
        plaintext=None,
        rendered_html=None,
        screenshot=None,
        download=None,
    )


async def _connect(playwright, browser_ws_url: str) -> Browser:
    """Connect to the remote browser server."""
    return await playwright.chromium.connect(
        browser_ws_url,
        # connect's timeout defaults to 0 (no timeout). Bound the handshake so
        # a dead server fails fast as a PlaywrightError instead of hanging.
        timeout=CONNECT_TIMEOUT_MS,
        headers=_launch_options_header(),
    )


async def _finalize_capture(
    context: BrowserContext,
    result: CaptureResult,
    http_archive_path: Path,
    url: str,
    storage: Storage,
) -> tuple[CaptureResult, dict | None]:
    """Close the context and process its HAR as an atomic evidence operation."""
    try:
        async with asyncio.timeout(CONTEXT_CLOSE_TIMEOUT_S):
            await context.close()
    except asyncio.TimeoutError:
        error = f"browser context close exceeded {CONTEXT_CLOSE_TIMEOUT_S}s budget"
        logger.error("%s for %s", error, url)
        return _empty_result(error), None
    except Exception as exception:
        error = f"browser context close failed: {exception}"
        logger.error("%s for %s", error, url)
        return _empty_result(error), None

    if result.http_status is None:
        return result, None
    if not http_archive_path.exists():
        error = "browser context closed without producing an HTTP archive"
        logger.error("%s for %s", error, url)
        return _empty_result(error), None

    async with asyncio.timeout(HAR_PROCESSING_TIMEOUT_S):
        http_archive = await capture_http_archive(
            http_archive_path, result.final_url, storage, download=result.download
        )
    if http_archive is None:
        error = "browser context produced an invalid HTTP archive"
        logger.error("%s for %s", error, url)
        return _empty_result(error), None
    return result, http_archive


async def _cleanup(browser: Browser | None, playwright) -> None:
    """Best-effort cleanup that never changes the capture outcome."""
    operations = []
    if browser is not None:
        operations.append(("browser.close", browser.close))
    if playwright is not None:
        operations.append(("playwright.stop", playwright.stop))

    for name, operation in operations:
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT_S):
                await operation()
        except asyncio.TimeoutError:
            logger.warning("%s exceeded %ss budget", name, CLEANUP_TIMEOUT_S)
        except Exception as error:
            logger.warning("%s failed: %s", name, error)


async def _persist_snapshot(
    url: str,
    result: CaptureResult,
    http_archive: dict | None,
    sessionmaker: async_sessionmaker,
    storage: Storage,
) -> Snapshot:
    """Commit captured evidence and return its public value."""
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
    async with asyncio.timeout(PERSIST_TIMEOUT_S):
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
) -> tuple[CaptureResult, dict | None]:
    """Capture and finalize one atomic evidence bundle."""
    playwright = None
    browser: Browser | None = None
    deadline = asyncio.timeout(SNAPSHOT_TIMEOUT_S)
    try:
        try:
            async with deadline:
                playwright = await async_playwright().start()
                browser = await _connect(playwright, browser_ws_url)

                http_archive_path = http_archive_dir / "record.zip"
                context = await browser.new_context(
                    record_har_path=str(http_archive_path),
                    record_har_content="attach",
                )
                page = await context.new_page()

                if drive is None:
                    result = await capture_page(page, url, storage)
                else:
                    result = await capture_driven(page, url, drive, storage)

                return await _finalize_capture(
                    context, result, http_archive_path, url, storage
                )
        except DriveTimeoutError as error:
            logger.error("%s for %s", error, url)
            return _empty_result(str(error)), None
        except PlaywrightError as error:
            logger.error("Browser error for %s: %s", url, error.message)
            return _empty_result(error.message), None
        except asyncio.TimeoutError:
            # Only the outer breaker is a persisted browser failure. Inner
            # storage and HAR-processing timeouts remain operational errors.
            if not deadline.expired():
                raise
            error = f"snapshot exceeded {SNAPSHOT_TIMEOUT_S}s wall-clock budget"
            logger.error("%s for %s", error, url)
            return _empty_result(error), None
    finally:
        await _cleanup(browser, playwright)


async def _capture(
    url: str,
    *,
    drive: DriveCallback | None,
    browser_ws_url: str,
    sessionmaker: async_sessionmaker,
    storage: Storage,
) -> Snapshot:
    """Capture *url* and persist the result."""
    logger.info("Capturing %s", url)

    http_archive_dir = Path(tempfile.mkdtemp())
    try:
        result, http_archive = await _capture_to_result(
            url,
            drive=drive,
            browser_ws_url=browser_ws_url,
            http_archive_dir=http_archive_dir,
            storage=storage,
        )
    finally:
        shutil.rmtree(http_archive_dir, ignore_errors=True)

    return await _persist_snapshot(url, result, http_archive, sessionmaker, storage)


class Pravda:
    """Configured async entry point for capture and snapshot history.

    Use as an async context manager. Each concurrent capture owns its browser
    connection, recording context, temporary directory, and database session.
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
        """Dispose the owned database engine."""
        await self._engine.dispose()

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
