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

# Timeout for navigation (reaching "commit" — first HTTP response received).
NAV_TIMEOUT_MS = 10_000

# Timeout for waiting on the normal load state after navigation.
LOAD_TIMEOUT_MS = 30_000

# Timeout for each individual capture operation (screenshot, etc.).
CAPTURE_TIMEOUT_MS = 15_000

# Timeout for CDP session creation, Page.stopLoading, and the DOM read
# (page.content) as one stage.
DOM_CAPTURE_TIMEOUT_S = 10

# Timeout for download event recovery and download.save_as().
DOWNLOAD_TIMEOUT_S = 15

# Hard bound on a caller's drive callback. The default (no-drive) path is
# bounded internally by capture_page; a drive callback is user code, so it
# gets an explicit budget. capture_current (DOM/screenshot/storage/download)
# runs after the callback and is bounded internally as in the default path.
# A timeout here is a capture-phase failure (no evidence yet).
DRIVE_TIMEOUT_S = 60


def is_http_url(url: str) -> bool:
    """Whether *url* is an HTTP(S) URL with a hostname."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and parsed.hostname is not None
    except ValueError:
        return False


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
    final_url: str | None
    plaintext: str | None
    rendered_html: str | None
    screenshot: str | None
    download: DownloadedBody | None


# An async callback that pilots a freshly created recording page: it owns
# every navigation and interaction (selectors, load states, clicks, form
# fills), using Playwright directly. Passed to :meth:`Pravda.snapshot` as
# ``drive``.
DriveCallback = Callable[[Page, str], Awaitable[None]]


class _PageObserver:
    """Track the first download and main-frame navigation responses on *page*.

    Both capture paths share exactly this need: the first ``download`` event
    (later downloads are ignored) and the status of each *main-frame*
    navigation response, so an iframe navigation can never overwrite the
    subject document's HTTP status. Used by the default :func:`capture_page`
    (where ``goto`` raising ``Download is starting`` prevents it from
    returning the status) and by :func:`capture_driven` (where the caller's
    callback performs arbitrary navigation).

    Install/remove is exception-safe: used as an async context manager,
    ``__aexit__`` always removes both listeners — on success, on a callback
    exception, and on cancellation — so no listener ever outlives the capture.
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        # The main frame is stable for a page's lifetime, so capturing it once
        # is enough to reject sub-frame navigation responses below.
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
        # Only the first download is the capture subject; ignore the rest.
        if self.download is None:
            self.download = download
            self._download_seen.set()

    def _on_response(self, response) -> None:
        # Main-frame navigation responses only: a sub-frame (iframe)
        # navigation must not overwrite the subject document's status.
        if (
            response.request.is_navigation_request()
            and response.frame is self._main_frame
        ):
            self.navigation_status = response.status

    async def wait_download(self, url: str) -> Download | None:
        """Await the first download event, bounded by DOWNLOAD_TIMEOUT_S.

        A navigation that handed off to Chrome's downloader (e.g. a PDF) lands
        the page at ``about:blank`` before the event fires; the observer stays
        installed while awaiting so the download becomes the captured subject.
        Returns the resolved download, or ``None`` (with a warning) when the
        event never fires within the budget. Safe to call after a download was
        already seen — the event stays set and it returns immediately.
        """
        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT_S):
                await self._download_seen.wait()
        except asyncio.TimeoutError:
            logger.warning("Download event did not fire for %s", url)
        return self.download


async def capture_page(page: Page, url: str, storage: Storage) -> CaptureResult:
    """Navigate to *url* and capture evidence: HTTP response and
    screenshot/HTML/text blobs.

    Readiness is a fixed internal default: navigate to ``commit`` (first
    response), then wait for the normal ``load`` state. Reading the status at
    commit — *before* the load wait — means a load timeout still records the
    HTTP response and any partial page evidence. This is an implementation
    detail of the default (no-``drive``) path, not public configuration;
    callers needing anything else pass a ``drive`` callback to
    :meth:`Pravda.snapshot` and use Playwright directly on the recording page.

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
    async with _PageObserver(page) as observer:
        navigation = await _navigate(page, url)

        # Defaults; the download branch overrides status/url, the
        # committed-navigation branch overrides the artifacts.
        downloaded: DownloadedBody | None = None
        http_status = navigation.http_status
        final_url = navigation.final_url
        plaintext = rendered_html = screenshot = None

        if navigation.is_download:
            # The navigation handed off as a download (e.g. a PDF). The body
            # never reaches the renderer, so there is nothing on the page to
            # capture; instead we read the download's bytes for the caller to
            # fold back into the HAR. The status/url come from the response
            # observer (``goto`` raised before returning) and the download.
            # With no download captured the page is left on about:blank — no
            # real landing URL — so final_url stays None rather than exposing
            # a non-HTTP(S) value.
            final_url = None
            download = await observer.wait_download(url)
            if download is not None:
                downloaded = await _save_download(download)
                if downloaded is not None:
                    http_status = observer.navigation_status
                    final_url = downloaded.url
        elif navigation.http_status is not None:
            plaintext, rendered_html, screenshot = await _capture_artifacts(
                page, navigation.final_url, storage
            )
        # else: navigation never committed — nothing on the page to capture.

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
    """Navigate to *url* and wait for the normal ``load`` state.

    Status is read at "commit" (first response), *before* the load wait — so
    a load timeout still records the HTTP response and lets the partial page
    evidence (DOM already parsed) be captured.
    """
    http_status: int | None = None
    final_url: str | None = None
    try:
        response = await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        http_status = response.status
        # page.url reflects any redirects that happened during navigation.
        final_url = page.url
        await page.wait_for_load_state("load", timeout=LOAD_TIMEOUT_MS)
        return _Navigation(
            http_status=http_status,
            error=None,
            final_url=final_url,
            is_download=False,
        )
    except PlaywrightError as exception:
        # Navigating to a URL that Chrome downloads (e.g. a PDF, once the
        # ``AlwaysOpenPdfExternally`` policy is active) makes ``page.goto``
        # raise "Download is starting" instead of returning a response. The
        # download event still fires, so the caller can capture its bytes;
        # there is no HTTP status to report (the response became a download).
        # ``page.goto`` and ``wait_for_load_state`` carry their own Playwright
        # timeouts (raising Playwright's TimeoutError, a PlaywrightError
        # subclass); they are not wrapped in asyncio.timeout, so no
        # asyncio.TimeoutError can escape here.
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


async def _store_blob(
    name: str, data: bytes, extension: str, url: str, storage: Storage
) -> str | None:
    """Store one artifact's bytes and return its content-addressed filename.

    Each write is individually bounded and isolated from the others: a write
    that exceeds its budget or fails operationally is logged and yields
    ``None`` for that artifact, so a wedged or erroring storage backend drops
    one artifact rather than the whole capture. Cancellation is never caught
    here — it propagates to the caller.
    """
    try:
        async with asyncio.timeout(STORAGE_WRITE_TIMEOUT_S):
            return await storage.put_blob(cas_name(data, extension), data, url)
    except asyncio.TimeoutError:
        logger.warning("Timeout storing %s for %s", name, url)
        return None
    except Exception as exception:
        logger.warning("Failed to store %s for %s: %s", name, url, exception)
        return None


async def _capture_artifacts(
    page: Page, url: str, storage: Storage
) -> tuple[str | None, str | None, str | None]:
    """Stop any pending requests, then capture the three artifacts.

    Stopping the page first forces it into a terminal, capturable state —
    otherwise the screenshot could stall on resources that never arrive.
    This mirrors hitting the browser's stop button. CDP session creation,
    ``Page.stopLoading``, and the DOM read (``page.content``) share one
    wall-clock budget so a stalled page cannot wedge the capture.

    ``plaintext`` is derived from the rendered HTML (via BeautifulSoup's
    ``get_text``) rather than the browser's ``innerText``. ``innerText`` is
    visible-only: it drops text inside ``display: none``, collapsed tabs,
    zero-size elements, and any JS-injected node CSS hides — content that
    nonetheless lands in ``rendered_html``. Extracting from the serialized
    DOM captures every text node, so plaintext reflects all the content the
    page has to offer (the same approach poliloom uses). Its content hash is
    therefore a reliable signal for whether a page's text changed and a
    downstream extraction needs re-running.

    Each storage write is individually bounded; a write that exceeds its
    budget or fails operationally yields ``None`` for that artifact and lets
    the others survive (see :func:`_store_blob`).

    Returns ``(plaintext, rendered_html, screenshot)`` filenames, each a
    content address ``<sha1>.<extension>`` whose extension carries its type;
    any individual capture that fails is ``None``.
    """
    try:
        async with asyncio.timeout(DOM_CAPTURE_TIMEOUT_S):
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Page.stopLoading", {})
            # Capture the rendered DOM, then derive plaintext from the same
            # source so both carry the full DOM text — including JS-injected
            # nodes hidden by CSS, which the browser's visible-only innerText
            # drops.
            html = await page.content()
    except (asyncio.TimeoutError, PlaywrightTimeout):
        logger.warning("Timeout capturing DOM content for %s", url)
        html = None
    except PlaywrightError as exception:
        logger.warning("Failed to capture DOM content for %s: %s", url, exception)
        html = None

    rendered_html = plaintext = None
    if html is not None:
        rendered_html = await _store_blob(
            "rendered_html", html.encode(), "html", url, storage
        )

        # Plaintext: every text node of the rendered DOM via get_text
        # (visible or not), whitespace collapsed so the hash tracks real
        # text changes — see the docstring for why this beats innerText.
        text = " ".join(
            BeautifulSoup(html, "html.parser").get_text(separator=" ").split()
        )
        plaintext = await _store_blob("plaintext", text.encode(), "txt", url, storage)

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
        storage,
    )

    return plaintext, rendered_html, screenshot


async def _capture_one(
    name: str, callback, url: str, extension: str, storage: Storage
) -> str | None:
    """Capture one artifact via *callback* and store the blob.

    The capture (``callback``) carries its own bound; timing out or raising a
    Playwright error yields ``None`` for this artifact. Storage is delegated
    to :func:`_store_blob`, which bounds the write and absorbs operational
    failures the same way.
    """
    try:
        data = await callback()
    except PlaywrightTimeout:
        logger.warning("Timeout capturing %s for %s", name, url)
        return None
    except PlaywrightError as exception:
        logger.warning("Failed to capture %s for %s: %s", name, url, exception)
        return None
    blob = data.encode() if isinstance(data, str) else data
    return await _store_blob(name, blob, extension, url, storage)


async def capture_current(
    page: Page,
    final_url: str,
    navigation_status: int | None,
    download: Download | None,
    storage: Storage,
) -> CaptureResult:
    """Capture evidence from the page's *current* state, without navigating.

    Used by the ``drive`` path of :meth:`Pravda.snapshot` (via
    :func:`capture_driven`): the caller has already navigated to and
    interacted with *page* via a ``drive`` callback. This captures the
    page-content artifacts (HTML/plaintext/screenshot) from the current DOM,
    records the main-document status the session observed
    (``navigation_status``) and the current URL (``final_url``), and — when a
    download fired during the session — recovers its bytes for the caller to
    fold back into the HAR.

    A download (e.g. a PDF the navigation handed off to) leaves the page at
    ``about:blank`` with nothing meaningful to render, so the page-content
    artifacts are skipped and only the download bytes are captured — matching
    the default PDF path. Returns the evidence as a ``CaptureResult``;
    storing it is the caller's job — this function never touches the database.
    """
    downloaded: DownloadedBody | None = None
    if download is not None:
        downloaded = await _save_download(download)

    plaintext = rendered_html = screenshot = None
    # Only capture page artifacts for a real committed document — not a
    # download (the page holds about:blank) nor a session that never
    # navigated (no document at all).
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
    """Run the caller's *drive* callback against *page*, then capture the
    resulting current page state.

    The latest main-frame document response and the first download are
    observed for the duration of the callback so :func:`capture_current` can
    record them. The callback owns every navigation and interaction —
    including catching a ``Download is starting`` error from a PDF-like
    navigation so the download can be recovered, just as the default path
    (:func:`capture_page`) does internally. It runs under
    :data:`DRIVE_TIMEOUT_S`; exceeding that budget raises
    ``asyncio.TimeoutError`` (the caller persists an empty failed attempt).

    A navigation that handed off to Chrome's downloader (e.g. a PDF) leaves
    the page at ``about:blank``; if the download event has not fired by the
    time the callback returns it is awaited (bounded) so the download becomes
    the captured subject. Returns the evidence as a ``CaptureResult``; the
    caller finalizes and persists.

    The callback must leave the page on an ``http(s)`` URL with a hostname.
    Ending on any other scheme (``about:``, ``data:``, ``file:``, ``blob:``)
    raises ``ValueError`` as callback misuse. A captured download becomes the
    subject and its URL must satisfy the same requirement.

    A Playwright error or timeout raised by *drive* propagates to the caller's
    failure handling (persisted as a failed attempt); any other exception
    raised by *drive* propagates untouched once the observers are removed.
    """
    async with _PageObserver(page) as observer:
        # The callback is user code: bound it so a wedged callback cannot hang
        # the capture. capture_current (below) is bounded internally.
        async with asyncio.timeout(DRIVE_TIMEOUT_S):
            await drive(page, url)

        if (
            page.url == "about:blank"
            and observer.navigation_status is None
            and observer.download is None
        ):
            raise ValueError(
                "drive returned before navigating the page; "
                "call page.goto(...) before returning"
            )

        # A navigation that handed off to Chrome's downloader (e.g. a PDF)
        # leaves the page at about:blank; the download event may follow the
        # callback's return. The observer stays installed while awaiting it so
        # the download becomes the captured subject.
        if observer.download is None and page.url == "about:blank":
            await observer.wait_download(url)

        final_url = observer.download.url if observer.download is not None else page.url

        # Pravda captures web evidence from HTTP(S) URLs only. This applies to
        # both the page left by the callback and a captured download URL.
        if not is_http_url(final_url):
            raise ValueError(
                f"drive ended on a non-HTTP(S) URL {final_url!r}; "
                "navigate to an http(s) URL before returning"
            )

        return await capture_current(
            page, final_url, observer.navigation_status, observer.download, storage
        )


async def _save_download(download: Download) -> DownloadedBody | None:
    """Save a resolved download's bytes as a ``DownloadedBody``.

    The body never reached the renderer (Chrome's viewer swallowed it), so we
    read it from the download instead. Returns ``None`` when the save exceeds
    its budget or fails operationally (including Playwright errors), so the
    caller can leave the matching HAR entry bodyless instead of dropping the
    whole archive.

    We connect to Chrome over WebSocket (a remote ``playwright run-server``),
    and ``download.path()`` throws in that mode, so we can't read the file
    Playwright already wrote — ``save_as`` is the only way to get the bytes.
    """
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
    except asyncio.TimeoutError:
        logger.warning("Timeout saving download for %s", download.url)
        return None
    except Exception as exception:
        logger.warning("Failed to save download for %s: %s", download.url, exception)
        return None
