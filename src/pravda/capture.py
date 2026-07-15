import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Download, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from pravda.storage import STORAGE_WRITE_TIMEOUT_S, Storage, cas_name

logger = logging.getLogger(__name__)

NAV_TIMEOUT_MS = 10_000
LOAD_TIMEOUT_MS = 30_000
CAPTURE_TIMEOUT_MS = 15_000
DOM_CAPTURE_TIMEOUT_S = 10
DOWNLOAD_TIMEOUT_S = 15
DRIVE_TIMEOUT_S = 60


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
    plaintext: str | None = None
    rendered_html: str | None = None
    screenshot: str | None = None
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

    async def wait_download(self, url: str) -> Download:
        """Wait up to ``DOWNLOAD_TIMEOUT_S`` for the first download."""
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT_S):
                await self._download_seen.wait()
        except asyncio.TimeoutError as error:
            raise PlaywrightError(f"Download event did not fire for {url}") from error
        assert self.download is not None
        return self.download


async def capture_page(page: Page, url: str, storage: Storage) -> CaptureResult:
    """Navigate to *url* and capture its status, page artifacts, or download."""
    async with _PageObserver(page) as observer:
        navigation = await _navigate(page, url)

        downloaded: DownloadedBody | None = None
        http_status = navigation.http_status
        final_url = navigation.final_url
        plaintext = rendered_html = screenshot = None

        if navigation.is_download:
            download = await observer.wait_download(url)
            downloaded = await _save_download(download)
            http_status = observer.navigation_status
            final_url = downloaded.url
        elif navigation.http_status is not None:
            plaintext, rendered_html, screenshot = await _capture_artifacts(
                page, navigation.final_url, storage
            )

        return CaptureResult(
            http_status=http_status,
            error=navigation.error,
            final_url=final_url,
            plaintext=plaintext,
            rendered_html=rendered_html,
            screenshot=screenshot,
            download=downloaded,
        )


@dataclass
class _Navigation:
    http_status: int | None
    error: str | None
    final_url: str | None
    is_download: bool


async def _navigate(page: Page, url: str) -> _Navigation:
    """Navigate to commit, retain its status, then wait for normal load."""
    http_status: int | None = None
    final_url: str | None = None
    try:
        response = await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        http_status = response.status
        final_url = page.url
        await page.wait_for_load_state("load", timeout=LOAD_TIMEOUT_MS)
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


async def _store_blob(data: bytes, extension: str, url: str, storage: Storage) -> str:
    """Store one artifact within the storage-write deadline."""
    async with asyncio.timeout(STORAGE_WRITE_TIMEOUT_S):
        return await storage.put_blob(cas_name(data, extension), data, url)


async def _capture_artifacts(
    page: Page, url: str, storage: Storage
) -> tuple[str | None, str | None, str | None]:
    """Stop loading and capture plaintext, rendered HTML, and screenshot."""
    try:
        async with asyncio.timeout(DOM_CAPTURE_TIMEOUT_S):
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Page.stopLoading", {})
            html = await page.content()
    except (asyncio.TimeoutError, PlaywrightTimeout):
        logger.warning("Timeout capturing DOM content for %s", url)
        html = None
    except PlaywrightError as exception:
        logger.warning("Failed to capture DOM content for %s: %s", url, exception)
        html = None

    rendered_html = plaintext = None
    if html is not None:
        rendered_html = await _store_blob(html.encode(), "html", url, storage)

        # Derive text from the DOM so hidden and injected nodes are included.
        text = " ".join(
            BeautifulSoup(html, "html.parser").get_text(separator=" ").split()
        )
        plaintext = await _store_blob(text.encode(), "txt", url, storage)

    # Clipping is the reliable way to cap full-page screenshots to viewport width.
    viewport_size = page.viewport_size
    screenshot_clip = (
        {"x": 0, "y": 0, "width": viewport_size["width"], "height": 1 << 30}
        if viewport_size
        else None
    )
    screenshot = await _capture_one(
        "screenshot",
        lambda: page.screenshot(
            full_page=True,
            clip=screenshot_clip,
            timeout=CAPTURE_TIMEOUT_MS,
        ),
        url,
        "png",
        storage,
    )

    return plaintext, rendered_html, screenshot


async def _capture_one(
    name: str, callback, url: str, extension: str, storage: Storage
) -> str | None:
    """Capture and store one artifact, returning ``None`` on capture failure."""
    try:
        data = await callback()
    except PlaywrightTimeout:
        logger.warning("Timeout capturing %s for %s", name, url)
        return None
    except PlaywrightError as exception:
        logger.warning("Failed to capture %s for %s: %s", name, url, exception)
        return None
    return await _store_blob(data, extension, url, storage)


async def capture_current(
    page: Page,
    final_url: str,
    navigation_status: int | None,
    download: Download | None,
    storage: Storage,
) -> CaptureResult:
    """Capture the current page or download without navigating."""
    downloaded: DownloadedBody | None = None
    if download is not None:
        downloaded = await _save_download(download)

    plaintext = rendered_html = screenshot = None
    if navigation_status is not None and download is None:
        plaintext, rendered_html, screenshot = await _capture_artifacts(
            page, final_url, storage
        )

    return CaptureResult(
        http_status=navigation_status,
        error=None,
        final_url=final_url,
        plaintext=plaintext,
        rendered_html=rendered_html,
        screenshot=screenshot,
        download=downloaded,
    )


async def capture_driven(
    page: Page, url: str, drive: DriveCallback, storage: Storage
) -> CaptureResult:
    """Run *drive*, then capture the resulting HTTP(S) page or download."""
    async with _PageObserver(page) as observer:
        try:
            async with asyncio.timeout(DRIVE_TIMEOUT_S):
                await drive(page, url)
        except asyncio.TimeoutError as error:
            raise DriveTimeoutError(
                f"drive callback exceeded {DRIVE_TIMEOUT_S}s budget"
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
            await observer.wait_download(url)

        download = observer.download
        final_url = download.url if download is not None else page.url

        if not is_http_url(final_url):
            raise ValueError(
                f"drive ended on a non-HTTP(S) URL {final_url!r}; "
                "navigate to an http(s) URL before returning"
            )

        return await capture_current(
            page, final_url, observer.navigation_status, download, storage
        )


async def _save_download(download: Download) -> DownloadedBody:
    """Save a remote download or propagate the recovery failure."""
    try:
        with tempfile.TemporaryDirectory() as download_dir:
            download_path = Path(download_dir) / "download"
            async with asyncio.timeout(DOWNLOAD_TIMEOUT_S):
                await download.save_as(str(download_path))
                return DownloadedBody(
                    url=download.url,
                    data=download_path.read_bytes(),
                    suggested_filename=download.suggested_filename,
                )
    except asyncio.TimeoutError as error:
        raise PlaywrightError(
            f"Download save exceeded {DOWNLOAD_TIMEOUT_S}s budget for {download.url}"
        ) from error
