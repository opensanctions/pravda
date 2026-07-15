"""Configured browser capture and snapshot history."""

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
    is_http_url,
)
from pravda.db import SnapshotRecord
from pravda.http_archive import capture_http_archive
from pravda.snapshots import Snapshot, from_record
from pravda.storage import Storage

logger = logging.getLogger(__name__)

BROWSER_CHANNEL = "chrome"

# Each pipeline phase has its own wall-clock limit. Browser context failures keep
# page evidence but discard the HAR; cleanup is best-effort. Storage, commit,
# non-Playwright callback failures, and cancellation propagate.
PLAYWRIGHT_START_TIMEOUT_S = 10
CONNECT_TIMEOUT_MS = 10_000
SETUP_TIMEOUT_S = 10
CONTEXT_CLOSE_TIMEOUT_S = 10
HAR_PROCESSING_TIMEOUT_S = 20
BROWSER_CLOSE_TIMEOUT_S = 5
PLAYWRIGHT_STOP_TIMEOUT_S = 5
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


async def _start_playwright(stages: dict[str, float]):
    """Start the Playwright driver within its deadline."""
    stage_start = time.monotonic()
    async with asyncio.timeout(PLAYWRIGHT_START_TIMEOUT_S):
        playwright = await async_playwright().start()
    stages["startup"] = time.monotonic() - stage_start
    return playwright


async def _stop_playwright(playwright, stages: dict[str, float]) -> None:
    """Stop the Playwright driver, logging cleanup failures."""
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
    """Create a HAR-recording context and page within the setup deadline."""
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
    """Append a finalization error to an existing capture error."""
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
    """Flush and process the HAR, preserving page evidence on failure."""
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

    if (
        result.http_status is None
        or http_archive_path is None
        or (not http_archive_path.exists())
    ):
        return result, None

    stage_start = time.monotonic()
    async with asyncio.timeout(HAR_PROCESSING_TIMEOUT_S):
        http_archive = await capture_http_archive(
            http_archive_path, result.final_url, storage, download=result.download
        )
    stages["har"] = time.monotonic() - stage_start
    return result, http_archive


async def _close_browser(browser: Browser) -> None:
    """Close a browser, logging cleanup failures."""
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
    """Capture and finalize evidence, then clean up browser resources."""
    try:
        playwright = await _start_playwright(stages)
    except Exception as error:
        logger.error("Playwright startup failed for %s: %s", url, error)
        return _empty_result(f"playwright startup failed: {error}"), None

    result = _empty_result()
    http_archive: dict | None = None
    browser: Browser | None = None
    try:
        timeout_message: str | None = None
        try:
            browser = await _connect(playwright, browser_ws_url, stages)
            timeout_message = f"context/page setup exceeded {SETUP_TIMEOUT_S}s budget"
            context, page, http_archive_path = await _setup(
                browser, http_archive_dir, stages
            )
            capture_start = time.monotonic()
            if drive is None:
                result = await capture_page(page, url, storage)
            else:
                timeout_message = f"drive callback exceeded {DRIVE_TIMEOUT_S}s budget"
                result = await capture_driven(page, url, drive, storage)
            stages["capture"] = time.monotonic() - capture_start
        except PlaywrightError as error:
            logger.error("Browser error for %s: %s", url, error.message)
            result = _empty_result(error.message)
        except asyncio.TimeoutError:
            message = timeout_message or "capture exceeded its budget"
            logger.error("%s for %s", message, url)
            result = _empty_result(message)
        else:
            result, http_archive = await _finalize_capture(
                context,
                result,
                http_archive_path,
                url,
                stages,
                storage,
            )
    finally:
        if browser is not None:
            await _close_browser(browser)
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
    """Capture *url* and persist the result."""
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
        shutil.rmtree(http_archive_dir, ignore_errors=True)

    return await _persist_snapshot(
        url, result, http_archive, stages, sessionmaker, storage
    )


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
        recording page; Pravda then captures its current state.

        Each phase is bounded. Playwright failures are persisted, while storage,
        non-Playwright callback, and persistence errors propagate. Browser
        context-finalization failures retain captured page evidence but discard
        the HAR.
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
