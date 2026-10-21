"""Configured browser capture."""

import asyncio
import logging
import shutil
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
from pravda.config import Liveness, PravdaConfig, Timeouts
from pravda.http_archive import capture_http_archive
from pravda.snapshots import Snapshot
from pravda.standalone import capture_standalone_html
from pravda.storage import Storage

logger = logging.getLogger(__name__)

# The return/raise boundary is the capture zone, not the failure's
# nature: navigation, capture, and Playwright callback failures are
# returned as failed snapshots — snapshot.error may carry browser noise
# like "Target crashed", since Playwright has one Error type. Browser
# setup and context close are machinery: they raise InfrastructureError
# and capture nothing. Evidence finalizes atomically, except
# standalone-HTML derivation failures, which drop only that artifact.


class InfrastructureError(Exception):
    """Pravda's browser machinery failed; nothing was captured."""


class _CaptureFailure(Exception):
    """The target-facing capture exceeded its wall-clock budget."""


@dataclass
class StoredArtifacts:
    """Stored artifact names and manifest produced by finalization."""

    rendered_html: str | None = None
    screenshot: str | None = None
    standalone_html: str | None = None
    http_archive: dict | None = None


async def _close_context(context: BrowserContext, liveness: Liveness) -> None:
    """Close the recording context, finalizing its HAR on disk."""
    try:
        async with asyncio.timeout(liveness.close):
            await context.close()
    except TimeoutError as error:
        raise InfrastructureError(
            f"browser context close exceeded {liveness.close:g}s budget"
        ) from error
    except Exception as error:
        raise InfrastructureError(f"browser context close failed: {error}") from error


async def _store_artifacts(
    result: CaptureResult, http_archive_path: Path, storage: Storage
) -> StoredArtifacts:
    """Store captured evidence; the context must be closed so its HAR is flushed."""
    if result.http_status is None:
        return StoredArtifacts()
    if not http_archive_path.exists():
        raise InfrastructureError(
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
        raise InfrastructureError("browser context produced an invalid HTTP archive")

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


async def _capture(
    url: str,
    *,
    drive: DriveCallback | None,
    browser_ws_url: str,
    storage: Storage,
    timeouts: Timeouts,
    liveness: Liveness,
) -> Snapshot:
    logger.info("Capturing %s", url)

    http_archive_dir = Path(tempfile.mkdtemp())
    try:
        async with async_playwright() as playwright:
            # No browser.close(): stopping the driver disconnects, and the
            # endpoint closes this connection's contexts and browsers on
            # disconnect.
            try:
                async with asyncio.timeout(liveness.setup):
                    browser = await playwright.chromium.connect(browser_ws_url)
                    http_archive_path = http_archive_dir / "record.zip"
                    context = await browser.new_context(
                        record_har_path=str(http_archive_path),
                        record_har_content="attach",
                    )
                    page = await context.new_page()
            except TimeoutError as error:
                raise InfrastructureError(
                    f"browser connection setup exceeded {liveness.setup:g}s budget"
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
            except (DriveTimeoutError, PlaywrightError, _CaptureFailure) as error:
                logger.error("Capture failed for %s: %s", url, error)
                result = CaptureResult(error=str(error))
                artifacts = StoredArtifacts()
            else:
                await _close_context(context, liveness)
                artifacts = await _store_artifacts(result, http_archive_path, storage)
    finally:
        shutil.rmtree(http_archive_dir, ignore_errors=True)

    snapshot = Snapshot(
        id=uuid.uuid4(),
        url=url,
        final_url=result.final_url,
        captured_at=datetime.now(timezone.utc),
        http_status=result.http_status,
        error=result.error,
        rendered_html=artifacts.rendered_html,
        screenshot=artifacts.screenshot,
        standalone_html=artifacts.standalone_html,
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
    context, and temporary directory. Captures return :class:`Snapshot`
    values; persisting them is the application's policy.
    """

    def __init__(self, config: PravdaConfig) -> None:
        self._timeouts = config.timeouts
        self._storage = Storage.from_url(config.storage_base_path)
        self._browser_ws_url = config.browser_ws_url
        self._liveness = config.liveness

    async def capture(
        self, url: str, *, drive: DriveCallback | None = None
    ) -> Snapshot:
        """Capture an HTTP(S) URL and return the snapshot value.

        Without *drive*, Pravda navigates and waits for normal page load. With
        *drive*, the callback owns navigation and interaction on the supplied
        recording page; Pravda then captures its current state. The first
        download triggered by the callback becomes the capture subject.

        The snapshot is a value: persisting it — including failed
        captures, returned with ``error`` set — is the caller's policy.

        Browser machinery failures — connecting, preparing, or closing the
        recording context — raise :class:`InfrastructureError` and capture
        nothing; a wedged-but-alive endpoint fails at the liveness deadlines
        on ``PravdaConfig.liveness`` (seconds, default 30 each). A
        wedged-but-alive storage backend hangs until the caller cancels,
        and cancellation captures nothing.
        """
        if not is_http_url(url):
            raise ValueError(f"capture URL must be http(s), got {url!r}")
        return await _capture(
            url,
            drive=drive,
            browser_ws_url=self._browser_ws_url,
            storage=self._storage,
            timeouts=self._timeouts,
            liveness=self._liveness,
        )
