import asyncio
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Download, Page
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from pravda.db import ConditionType
from pravda.storage import cas_name, put_blob

logger = logging.getLogger(__name__)

# Timeout for navigation (reaching "commit" — first HTTP response received).
NAV_TIMEOUT_MS = 10_000

# Timeout for waiting on the page condition after navigation.
CONDITION_TIMEOUT_MS = 30_000

# Timeout for each individual capture operation (screenshot, etc.).
CAPTURE_TIMEOUT_MS = 15_000


@dataclass
class DownloadedBody:
    """Body of a response Chrome downloaded instead of rendering.

    Chrome's viewer extensions (e.g. the PDF viewer) consume certain response
    streams, so the body never reaches the renderer or the HAR. With the
    ``AlwaysOpenPdfExternally`` policy active, Chrome downloads such responses
    instead and Playwright fires a ``download`` event yielding the real bytes.
    The caller folds these back into the HAR as a ``content._file`` entry, so
    the download is indistinguishable from any other captured body.

    ``suggested_filename`` is the name Chrome itself chose for the download
    (from the server's ``Content-Disposition``, the URL, or its default) — we
    reuse its extension for the content-addressed blob.
    """

    url: str
    data: bytes
    suggested_filename: str


@dataclass
class CaptureResult:
    """Pure evidence captured from a page — no persistence concerns."""

    http_status: int | None
    error: str | None
    condition_met: bool
    final_url: str | None
    plaintext: str | None
    rendered_html: str | None
    screenshot: str | None
    download: DownloadedBody | None


async def capture_page(
    page: Page,
    url: str,
    condition_type: ConditionType = ConditionType.lifecycle,
    condition: str = "load",
    condition_timeout_ms: int = CONDITION_TIMEOUT_MS,
) -> CaptureResult:
    """Navigate to *url* and capture evidence: HTTP response and
    screenshot/HTML/text blobs.

    The network archive (a HAR recording) is not captured here — it is bound
    to the browser context's lifecycle, so the caller (which owns the
    context) is responsible for it.

    A URL that serves a PDF is special: Chrome's built-in PDF viewer
    consumes the response stream, so the body never reaches the renderer.
    With the ``AlwaysOpenPdfExternally`` policy baked into the browser image,
    Chrome downloads it instead and Playwright fires a ``download`` event —
    we recover those bytes and surface them as a ``DownloadedBody`` for the
    caller to fold back into the HAR, skipping the (empty) page-content
    captures.

    Returns the evidence as a ``CaptureResult``. Storing it is the caller's
    job — this function never touches the database.
    """
    loop = asyncio.get_running_loop()
    download_future: asyncio.Future[Download] = loop.create_future()
    navigation_status: int | None = None

    def _record_download(download: Download) -> None:
        if not download_future.done():
            download_future.set_result(download)

    def _record_response(response) -> None:
        # ``goto`` itself returns the status for normal navigations. This
        # listener is only the *fallback* for the download case: ``goto``
        # raises "Download is starting" before it can return a response, so
        # we capture the main document's status here while it still arrives.
        nonlocal navigation_status
        if response.request.is_navigation_request():
            navigation_status = response.status

    page.on("download", _record_download)
    page.on("response", _record_response)
    try:
        navigation = await _navigate(
            page, url, condition_type, condition, condition_timeout_ms
        )

        # Defaults shared by every branch; the download branch overrides.
        downloaded: DownloadedBody | None = None
        http_status = navigation.http_status
        final_url = navigation.final_url

        if navigation.is_download:
            # The navigation handed off as a download (e.g. a PDF). The body
            # never reaches the renderer, so there is nothing on the page to
            # capture; instead we read the download's bytes for the caller to
            # fold back into the HAR. The status/url come from the response
            # listener (``goto`` raised before returning) and the download.
            downloaded = await _recover_download(download_future, url)
            if downloaded is not None:
                http_status = navigation_status
                final_url = downloaded.url
            artifacts = CapturedArtifacts(None, None, None)
        elif navigation.http_status is not None:
            artifacts = await _capture_artifacts(page, navigation.final_url)
        else:
            # Navigation never committed — there is nothing on the page to capture.
            artifacts = CapturedArtifacts(None, None, None)

        return CaptureResult(
            http_status=http_status,
            error=navigation.error,
            condition_met=navigation.condition_met,
            final_url=final_url,
            plaintext=artifacts.plaintext,
            rendered_html=artifacts.rendered_html,
            screenshot=artifacts.screenshot,
            download=downloaded,
        )
    finally:
        page.remove_listener("download", _record_download)
        page.remove_listener("response", _record_response)


@dataclass
class _Navigation:
    http_status: int | None
    condition_met: bool
    error: str | None
    final_url: str | None
    is_download: bool


async def _navigate(
    page: Page,
    url: str,
    condition_type: ConditionType,
    condition: str,
    condition_timeout_ms: int,
) -> _Navigation:
    """Navigate to *url*, then wait for the requested condition.

    Status is read at "commit" (first response), *before* the condition
    wait — so a condition timeout still records the HTTP response.

    Lifecycle conditions use Playwright's ``wait_for_load_state`` ("commit"
    needs no extra wait — ``page.goto`` already waited for it); selector
    conditions use ``wait_for_selector``.
    """
    http_status: int | None = None
    final_url: str | None = None
    try:
        response = await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        http_status = response.status
        # page.url reflects any redirects that happened during navigation.
        final_url = page.url

        if condition_type is ConditionType.selector:
            await page.wait_for_selector(condition, timeout=condition_timeout_ms)
        elif condition != "commit":
            await page.wait_for_load_state(condition, timeout=condition_timeout_ms)

        return _Navigation(
            http_status,
            condition_met=True,
            error=None,
            final_url=final_url,
            is_download=False,
        )
    except (PlaywrightError, asyncio.TimeoutError) as exception:
        # Navigating to a URL that Chrome downloads (e.g. a PDF, once the
        # ``AlwaysOpenPdfExternally`` policy is active) makes ``page.goto``
        # raise "Download is starting" instead of returning a response. The
        # download event still fires, so the caller can capture its bytes;
        # there is no HTTP status to report (the response became a download).
        if isinstance(exception, PlaywrightError) and "Download is starting" in (
            exception.message or ""
        ):
            logger.info("Navigation became a download for %s", url)
            return _Navigation(
                http_status=None,
                condition_met=True,
                error=None,
                final_url=page.url or url,
                is_download=True,
            )
        error = str(exception) or (
            f"Timeout {condition_timeout_ms}ms exceeded waiting for '{condition}'"
        )
        logger.warning(
            "Timeout for %s (condition_type=%s, condition=%s): %s",
            url,
            condition_type.value,
            condition,
            error,
        )
        return _Navigation(
            http_status,
            condition_met=False,
            error=error,
            final_url=final_url,
            is_download=False,
        )


@dataclass
class CapturedArtifacts:
    """Filenames of the three per-page captured artifacts.

    Each is a content address ``<sha1>.<extension>``; the extension
    (txt/html/png) carries the artifact's type.
    """

    plaintext: str | None
    rendered_html: str | None
    screenshot: str | None


async def _capture_artifacts(page: Page, url: str) -> CapturedArtifacts:
    """Stop any pending requests, then capture the three artifacts.

    Stopping the page first forces it into a terminal, capturable state —
    otherwise the screenshot could stall on resources that never arrive.
    This mirrors hitting the browser's stop button.
    """
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Page.stopLoading", {})

    plaintext = await _capture_one(
        "plaintext",
        lambda: page.inner_text("body", timeout=CAPTURE_TIMEOUT_MS),
        url,
        "txt",
    )
    rendered_html = await _capture_one(
        "rendered_html",
        lambda: page.content(),
        url,
        "html",
    )
    # Use clip to constrain the screenshot width to the viewport width.
    # CSS approaches (max-width on html/body, overflow-x: hidden, etc.) don't
    # work because Playwright measures scrollWidth, which reports the full
    # content width regardless of overflow settings. Clipping the output image
    # is the only reliable way to cap the width.
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
    )

    return CapturedArtifacts(
        plaintext=plaintext,
        rendered_html=rendered_html,
        screenshot=screenshot,
    )


async def _capture_one(name: str, callback, url: str, extension: str) -> str | None:
    """Capture one artifact via *callback* and store the blob."""
    try:
        data = await callback()
        if isinstance(data, str):
            data = data.encode()
        name = cas_name(data, extension)
        return await put_blob(name, data, url)
    except (asyncio.TimeoutError, PlaywrightTimeout):
        logger.warning("Timeout capturing %s for %s", name, url)
        return None
    except Exception as exception:
        logger.warning("Failed to capture %s for %s: %s", name, url, exception)
        return None


async def _recover_download(
    download_future: "asyncio.Future[Download]", url: str
) -> DownloadedBody | None:
    """Recover the bytes of a navigation that handed off as a download.

    The body never reached the renderer (Chrome's viewer swallowed it), so we
    read it from the ``download`` event instead. Returns ``None`` when the
    event never fires or yields no bytes.

    We connect to Chrome over WebSocket (a remote ``playwright run-server``),
    and ``download.path()`` throws in that mode, so we can't read the file
    Playwright already wrote — ``save_as`` is the only way to get the bytes.
    """
    try:
        download = await asyncio.wait_for(
            download_future, timeout=CAPTURE_TIMEOUT_MS / 1000
        )
    except asyncio.TimeoutError:
        logger.warning("Download event did not fire for %s", url)
        return None

    download_dir = Path(tempfile.mkdtemp())
    try:
        await download.save_as(str(download_dir / "download"))
        return DownloadedBody(
            url=download.url,
            data=(download_dir / "download").read_bytes(),
            suggested_filename=download.suggested_filename,
        )
    except Exception as exception:
        logger.warning("Failed to save download for %s: %s", download.url, exception)
        return None
    finally:
        shutil.rmtree(download_dir, ignore_errors=True)
