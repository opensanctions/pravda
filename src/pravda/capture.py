import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Download, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from pravda.config import Timeouts

logger = logging.getLogger(__name__)


class DriveTimeoutError(Exception):
    """The caller's drive callback exceeded its wall-clock budget."""


def is_http_url(url: str) -> bool:
    """Whether *url* is an HTTP(S) URL with a hostname."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.hostname is not None
    except ValueError:
        return False


@dataclass
class DownloadedBody:
    """A response Chrome downloaded instead of rendering or recording."""

    url: str
    data: bytes
    suggested_filename: str


@dataclass
class CaptureResult:
    """Pure evidence captured from a page — no persistence concerns."""

    http_status: int | None = None
    error: str | None = None
    final_url: str | None = None
    html: str | None = None
    screenshot: bytes | None = None
    download: DownloadedBody | None = None


# A callback that navigates and interacts with a fresh recording page.
DriveCallback = Callable[[Page, str], Awaitable[None]]


class _PageObserver:
    """Track the first download and main-frame navigation status."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self._main_frame = page.main_frame
        self._download_seen = asyncio.Event()
        self.navigation_status: int | None = None
        self.download: Download | None = None

    async def __aenter__(self) -> "_PageObserver":
        self._page.on("download", self._on_download)
        self._page.on("response", self._on_response)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._page.remove_listener("download", self._on_download)
        self._page.remove_listener("response", self._on_response)

    def _on_download(self, download: Download) -> None:
        if self.download is None:
            self.download = download
            self._download_seen.set()

    def _on_response(self, response) -> None:
        if (
            response.request.is_navigation_request()
            and response.frame is self._main_frame
        ):
            self.navigation_status = response.status

    async def wait_download(self, url: str, timeouts: Timeouts) -> Download:
        """Wait up to the download budget for the first download."""
        try:
            async with asyncio.timeout(timeouts.download):
                await self._download_seen.wait()
        except asyncio.TimeoutError as error:
            raise PlaywrightError(f"Download event did not fire for {url}") from error
        assert self.download is not None
        return self.download


async def capture_page(page: Page, url: str, timeouts: Timeouts) -> CaptureResult:
    """Navigate to *url* and capture its status, page artifacts, or download."""
    async with _PageObserver(page) as observer:
        navigation = await _navigate(page, url, timeouts)

        downloaded: DownloadedBody | None = None
        http_status = navigation.http_status
        final_url = navigation.final_url
        html = screenshot = None

        if navigation.is_download:
            download = await observer.wait_download(url, timeouts)
            downloaded = await _save_download(download, timeouts)
            http_status = observer.navigation_status
            final_url = downloaded.url
        elif navigation.http_status is not None:
            html, screenshot = await _capture_artifacts(page, timeouts)

        return CaptureResult(
            http_status=http_status,
            error=navigation.error,
            final_url=final_url,
            html=html,
            screenshot=screenshot,
            download=downloaded,
        )


@dataclass
class _Navigation:
    http_status: int | None
    error: str | None
    final_url: str | None
    is_download: bool


async def _navigate(page: Page, url: str, timeouts: Timeouts) -> _Navigation:
    """Navigate to commit, retain its status, then wait for normal load."""
    http_status: int | None = None
    final_url: str | None = None
    try:
        response = await page.goto(
            url, wait_until="commit", timeout=timeouts.nav * 1000
        )
        http_status = response.status
        final_url = page.url
        await page.wait_for_load_state("load", timeout=timeouts.load * 1000)
        return _Navigation(
            http_status=http_status,
            error=None,
            final_url=final_url,
            is_download=False,
        )
    except PlaywrightError as exception:
        if "Download is starting" in (exception.message or ""):
            logger.info("Navigation became a download for %s", url)
            return _Navigation(
                http_status=None,
                error=None,
                final_url=page.url or url,
                is_download=True,
            )
        error = str(exception)
        logger.warning("Navigation error for %s: %s", url, error)
        return _Navigation(
            http_status=http_status,
            error=error,
            final_url=final_url,
            is_download=False,
        )


async def _capture_artifacts(
    page: Page, timeouts: Timeouts
) -> tuple[str | None, bytes | None]:
    """Wait for the DOM to settle, then capture its HTML and screenshot."""
    try:
        async with asyncio.timeout(timeouts.dom_capture):
            # Attribute changes re-arm the quiet timer alongside structural ones:
            # late framework re-renders toggle layout classes long after load.
            await page.evaluate(
                """
                (quietMs) => new Promise((resolve) => {
                    let timer = setTimeout(settled, quietMs);
                    const observer = new MutationObserver(() => {
                        clearTimeout(timer);
                        timer = setTimeout(settled, quietMs);
                    });
                    function settled() {
                        observer.disconnect();
                        resolve();
                    }
                    observer.observe(document, {
                        childList: true,
                        subtree: true,
                        attributes: true,
                    });
                })
                """,
                timeouts.dom_settle_quiet * 1000,
            )
            html = await page.content()
    except (asyncio.TimeoutError, PlaywrightTimeout):
        logger.warning("Timeout capturing DOM content for %s", page.url)
        html = None
    except PlaywrightError as exception:
        logger.warning("Failed to capture DOM content for %s: %s", page.url, exception)
        html = None

    # Clipping is the reliable way to cap full-page screenshots to viewport width.
    viewport_size = page.viewport_size
    screenshot_clip = (
        {"x": 0, "y": 0, "width": viewport_size["width"], "height": 1 << 30}
        if viewport_size
        else None
    )
    try:
        screenshot = await page.screenshot(
            full_page=True,
            clip=screenshot_clip,
            timeout=timeouts.screenshot * 1000,
        )
    except PlaywrightTimeout:
        logger.warning("Timeout capturing screenshot for %s", page.url)
        screenshot = None
    except PlaywrightError as exception:
        logger.warning("Failed to capture screenshot for %s: %s", page.url, exception)
        screenshot = None

    return html, screenshot


async def capture_current(
    page: Page,
    final_url: str,
    navigation_status: int | None,
    download: Download | None,
    timeouts: Timeouts,
) -> CaptureResult:
    """Capture the current page or download without navigating."""
    downloaded: DownloadedBody | None = None
    if download is not None:
        downloaded = await _save_download(download, timeouts)

    html = screenshot = None
    if navigation_status is not None and download is None:
        html, screenshot = await _capture_artifacts(page, timeouts)

    return CaptureResult(
        http_status=navigation_status,
        error=None,
        final_url=final_url,
        html=html,
        screenshot=screenshot,
        download=downloaded,
    )


async def capture_driven(
    page: Page, url: str, drive: DriveCallback, timeouts: Timeouts
) -> CaptureResult:
    """Run *drive*, then capture the resulting HTTP(S) page or download."""
    async with _PageObserver(page) as observer:
        try:
            async with asyncio.timeout(timeouts.drive):
                await drive(page, url)
        except asyncio.TimeoutError as error:
            raise DriveTimeoutError(
                f"drive callback exceeded {timeouts.drive:g}s budget"
            ) from error

        if (
            page.url == "about:blank"
            and observer.navigation_status is None
            and observer.download is None
        ):
            raise ValueError(
                "drive returned before navigating the page; "
                "call page.goto(...) before returning"
            )

        if observer.download is None and page.url == "about:blank":
            await observer.wait_download(url, timeouts)

        download = observer.download
        final_url = download.url if download is not None else page.url

        if not is_http_url(final_url):
            raise ValueError(
                f"drive ended on a non-HTTP(S) URL {final_url!r}; "
                "navigate to an http(s) URL before returning"
            )

        return await capture_current(
            page, final_url, observer.navigation_status, download, timeouts
        )


async def _save_download(download: Download, timeouts: Timeouts) -> DownloadedBody:
    """Save a remote download or propagate the recovery failure."""
    try:
        with tempfile.TemporaryDirectory() as download_dir:
            download_path = Path(download_dir) / "download"
            async with asyncio.timeout(timeouts.download):
                await download.save_as(str(download_path))
                return DownloadedBody(
                    url=download.url,
                    data=download_path.read_bytes(),
                    suggested_filename=download.suggested_filename,
                )
    except asyncio.TimeoutError as error:
        raise PlaywrightError(
            f"Download save exceeded {timeouts.download:g}s budget for {download.url}"
        ) from error
