"""Configured browser capture and snapshot history."""

import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright
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

# Dead infrastructure fails promptly on its own errors; connect's timeout is
# the one place Playwright would leave us unbounded. It defaults to 0 — no
# timeout — so a black-holed handshake would wait ~2^31-1ms.
_CONNECT_TIMEOUT_MS = 10_000

# Capture and finalization form one atomic evidence operation: a context-close
# failure invalidates the result, while standalone-HTML derivation failures
# drop only that artifact.


class _CaptureFailure(Exception):
    """A browser failure that should be persisted without evidence."""


@dataclass
class StoredArtifacts:
    """Stored artifact names and manifest produced by finalization."""

    rendered_html: str | None = None
    screenshot: str | None = None
    standalone_html: str | None = None
    http_archive: dict | None = None


async def _finalize_capture(
    context: BrowserContext,
    result: CaptureResult,
    http_archive_path: Path,
    storage: Storage,
) -> StoredArtifacts:
    try:
        await context.close()
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

    http_archive = await capture_http_archive(
        http_archive_path, result.final_url, storage, download=result.download
    )
    if http_archive is None:
        raise _CaptureFailure("browser context produced an invalid HTTP archive")

    standalone_html = None
    if result.html is not None:
        try:
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


async def _persist_snapshot(
    url: str,
    result: CaptureResult,
    artifacts: StoredArtifacts,
    sessionmaker: async_sessionmaker,
    storage: Storage,
) -> Snapshot:
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
    # No browser.close(): stopping the driver disconnects, and the endpoint
    # closes this connection's contexts and browsers on disconnect.
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect(
            browser_ws_url, timeout=_CONNECT_TIMEOUT_MS
        )

        http_archive_path = http_archive_dir / "record.zip"
        context = await browser.new_context(
            record_har_path=str(http_archive_path),
            record_har_content="attach",
        )
        page = await context.new_page()

        try:
            async with asyncio.timeout(timeouts.snapshot_milliseconds / 1000):
                if drive is None:
                    result = await capture_page(page, url, timeouts)
                else:
                    result = await capture_driven(page, url, drive, timeouts)
        except asyncio.TimeoutError as error:
            raise _CaptureFailure(
                f"capture exceeded {timeouts.snapshot_milliseconds:g}ms"
                " wall-clock budget"
            ) from error

        artifacts = await _finalize_capture(context, result, http_archive_path, storage)
        return result, artifacts


async def _capture(
    url: str,
    *,
    drive: DriveCallback | None,
    browser_ws_url: str,
    sessionmaker: async_sessionmaker,
    storage: Storage,
    timeouts: Timeouts,
) -> Snapshot:
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

    return await _persist_snapshot(url, result, artifacts, sessionmaker, storage)


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

        A wedged-but-alive storage backend or database hangs until the caller
        cancels, and cancellation persists nothing.
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
