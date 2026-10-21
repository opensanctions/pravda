"""Configured browser capture."""

import asyncio
import logging
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright
from playwright.async_api import Error as PlaywrightError

from pravda.capture import (
    CaptureResult,
    DriveCallback,
    DriveTimeoutError,
    capture_driven,
    capture_page,
    is_http_url,
)
from pravda.config import Timeouts
from pravda.http_archive import capture_http_archive
from pravda.snapshots import Snapshot
from pravda.storage import Storage

logger = logging.getLogger(__name__)

# Playwright has one Error type, so capture failures are returned as
# failed snapshots rather than raised: page failure and browser noise
# ("Target crashed") can't be told apart.

# Browser setup is Pravda's machinery, not a knob: one fixed budget, in
# seconds.
_SETUP_BUDGET = 30.0


class InfrastructureError(Exception):
    """The browser endpoint never came up; nothing about the URL was attempted."""


class _CaptureFailure(Exception):
    """The capture exceeded its wall-clock budget."""


class _FinalizationError(Exception):
    """The recording could not be finalized."""


@dataclass
class StoredArtifacts:
    """Stored artifact paths and manifest produced by finalization."""

    rendered_html: str | None = None
    screenshot: str | None = None
    http_archive: dict | None = None


async def _close_context(context: BrowserContext, timeouts: Timeouts) -> None:
    """Close the recording context, flushing its HAR to disk."""
    try:
        async with asyncio.timeout(timeouts.close):
            await context.close()
    except TimeoutError as error:
        raise _FinalizationError(
            f"browser context close exceeded {timeouts.close:g}s budget"
        ) from error
    except Exception as error:
        raise _FinalizationError(f"browser context close failed: {error}") from error


async def _store_artifacts(
    result: CaptureResult, http_archive_path: Path, storage: Storage
) -> StoredArtifacts:
    """Store captured evidence; the context must be closed so its HAR is flushed."""
    if result.http_status is None:
        return StoredArtifacts()
    if not http_archive_path.exists():
        raise _FinalizationError(
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
        raise _FinalizationError("browser context produced an invalid HTTP archive")

    return StoredArtifacts(
        rendered_html=rendered_html,
        screenshot=screenshot,
        http_archive=http_archive,
    )


async def _capture(
    url: str,
    *,
    drive: DriveCallback | None,
    browser_ws_url: str,
    storage: Storage,
    timeouts: Timeouts,
) -> Snapshot:
    logger.info("Capturing %s", url)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        http_archive_path = Path(tmp) / "record.zip"
        async with async_playwright() as playwright:
            # No browser.close(): stopping the driver disconnects, and the
            # endpoint closes this connection's contexts and browsers on
            # disconnect.
            try:
                async with asyncio.timeout(_SETUP_BUDGET):
                    browser = await playwright.chromium.connect(browser_ws_url)
                    context = await browser.new_context(
                        record_har_path=str(http_archive_path),
                        record_har_content="attach",
                    )
                    page = await context.new_page()
            except TimeoutError as error:
                raise InfrastructureError(
                    f"browser connection setup exceeded {_SETUP_BUDGET:g}s budget"
                ) from error
            except Exception as error:
                raise InfrastructureError(
                    f"browser connection setup failed: {error}"
                ) from error

            try:
                try:
                    async with asyncio.timeout(timeouts.snapshot):
                        if drive is None:
                            result = await capture_page(page, url, timeouts)
                        else:
                            result = await capture_driven(page, url, drive, timeouts)
                except asyncio.TimeoutError as error:
                    raise _CaptureFailure(
                        f"capture exceeded {timeouts.snapshot:g}s wall-clock budget"
                    ) from error
                await _close_context(context, timeouts)
                artifacts = await _store_artifacts(result, http_archive_path, storage)
            except (
                DriveTimeoutError,
                PlaywrightError,
                _CaptureFailure,
                _FinalizationError,
            ) as error:
                logger.error("Capture failed for %s: %s", url, error)
                result = CaptureResult(error=str(error))
                artifacts = StoredArtifacts()

    snapshot = Snapshot(
        id=uuid.uuid4(),
        url=url,
        final_url=result.final_url,
        captured_at=datetime.now(timezone.utc),
        http_status=result.http_status,
        error=result.error,
        rendered_html=artifacts.rendered_html,
        screenshot=artifacts.screenshot,
        http_archive=artifacts.http_archive,
    )
    logger.info(
        "Captured %s id=%s: status=%s http_archive=%s error=%s",
        snapshot.url,
        snapshot.id,
        snapshot.http_status,
        snapshot.http_archive is not None,
        snapshot.error,
    )
    return snapshot


class Pravda:
    """Configured async entry point for evidence capture.

    Each concurrent capture owns its browser connection, recording
    context, and temporary directory.
    """

    def __init__(
        self,
        browser_ws_url: str,
        storage: Storage,
        *,
        timeouts: Timeouts = Timeouts(),
    ) -> None:
        self._timeouts = timeouts
        self._storage = storage
        self._browser_ws_url = browser_ws_url

    async def capture(
        self, url: str, *, drive: DriveCallback | None = None
    ) -> Snapshot:
        """Capture an HTTP(S) URL and return the snapshot value.

        Without *drive*, Pravda navigates and waits for normal page load. With
        *drive*, the callback owns navigation and interaction on the supplied
        recording page; Pravda then captures its current state. The first
        download triggered by the callback becomes the capture subject.

        The snapshot is a value: a failed capture is returned with
        ``error`` set rather than raised; persisting snapshots is the
        caller's policy.
        """
        if not is_http_url(url):
            raise ValueError(f"capture URL must be http(s), got {url!r}")
        return await _capture(
            url,
            drive=drive,
            browser_ws_url=self._browser_ws_url,
            storage=self._storage,
            timeouts=self._timeouts,
        )
