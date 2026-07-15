import asyncio
from pathlib import Path

import pytest
from playwright.async_api import Browser, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

import pravda.capture as capture_module
from pravda.capture import _save_download, capture_page
from pravda.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
async def page(browser: Browser):
    context = await browser.new_context()
    page = await context.new_page()
    try:
        yield page
    finally:
        await context.close()


@pytest.mark.asyncio
async def test_capture_page_returns_evidence(page: Page, storage: Storage):
    """Capture a page using a routed fixture and inspect the evidence."""
    fixture_html = (FIXTURES / "example.html").read_text()

    # Serve the fixture instead of making a real network request
    await page.route(
        "https://example.com",
        lambda route: route.fulfill(
            body=fixture_html,
            headers={"content-type": "text/html"},
        ),
    )

    result = await capture_page(page, "https://example.com", storage)

    assert result.http_status == 200
    assert result.error is None
    assert result.final_url == "https://example.com/"

    # All three per-page artifacts captured, each a <sha1>.<ext> filename.
    # The HAR is a context-lifecycle concern handled by session.finalize
    # (flushed when the recording context closes), so capture_page does not
    # touch it.
    assert result.plaintext.endswith(".txt")
    assert result.rendered_html.endswith(".html")
    assert result.screenshot.endswith(".png")


@pytest.mark.asyncio
async def test_capture_page_downloads_pdf(page: Page, storage: Storage):
    """A URL serving application/pdf is captured as a downloaded body.

    Chrome's PDF viewer would eat the response body, but the
    ``AlwaysOpenPdfExternally`` policy (baked into the browser image) makes
    Chrome download it instead. Playwright fires a ``download`` event and we
    recover the real bytes; the caller folds them back into the HAR, so no
    dedicated PDF field is needed.
    """
    fixture_pdf = (FIXTURES / "sample.pdf").read_bytes()

    await page.route(
        "https://example.com/doc.pdf",
        lambda route: route.fulfill(
            body=fixture_pdf,
            headers={"content-type": "application/pdf"},
        ),
    )

    result = await capture_page(page, "https://example.com/doc.pdf", storage)

    assert result.http_status == 200
    assert result.error is None
    assert result.final_url == "https://example.com/doc.pdf"

    # The PDF bytes were recovered from the download; the tab held nothing
    # meaningful, so the page-content artifacts are skipped.
    assert result.download is not None
    assert result.download.url == "https://example.com/doc.pdf"
    assert result.download.data == fixture_pdf
    assert result.plaintext is None
    assert result.rendered_html is None
    assert result.screenshot is None


@pytest.mark.asyncio
async def test_capture_page_goto_timeout_skips_captures(page: Page, storage: Storage):
    """A navigation that never commits skips captures entirely."""

    # Mock goto to raise timeout immediately — the page never commits.
    async def fake_goto(*args, **kwargs):
        raise PlaywrightTimeout("Navigation timeout")

    page.goto = fake_goto

    result = await capture_page(page, "https://timeout.example.com", storage)

    assert result.http_status is None  # unknown — goto never returned
    assert result.error is not None  # Playwright timeout message
    assert result.final_url is None

    # Navigation never committed, so there is nothing to capture
    assert result.plaintext is None
    assert result.rendered_html is None
    assert result.screenshot is None


@pytest.mark.asyncio
async def test_http_commit_captured_when_load_times_out(
    page: Page, storage: Storage, monkeypatch
):
    """HTTP status comes from commit; load times out.

    The two-step navigation means we get the HTTP response even when the
    page never finishes loading. Captures still run because DOMContentLoaded
    fires (the DOM parses fine — only the `load` event stalls on the image).
    """
    # Hang the image so `load` never fires, but serve the HTML fine.
    await page.route(
        "https://slow.example.com/slow-resource.png",
        lambda route: asyncio.Event().wait(),  # never resolves
    )
    await page.route(
        "https://slow.example.com",
        lambda route: route.fulfill(
            body=(FIXTURES / "blocking.html").read_text(),
            headers={"content-type": "text/html"},
        ),
    )

    # `load` waits on the blocked image and times out; DOMContentLoaded fires
    # almost immediately. A short timeout just keeps the test fast.
    monkeypatch.setattr(capture_module, "LOAD_TIMEOUT_MS", 2000)
    result = await capture_page(page, "https://slow.example.com", storage)

    # HTTP response was captured from the commit step
    assert result.http_status == 200
    assert result.final_url == "https://slow.example.com/"

    # load timed out
    assert result.error is not None

    # Navigation committed, so every capture ran. The screenshot went
    # through despite load timing out: pending requests are stopped first so
    # the page settles into a capturable state.
    assert result.plaintext is not None
    assert result.rendered_html is not None
    assert result.screenshot is not None


@pytest.mark.asyncio
async def test_capture_page_dom_capture_timeout_skips_content(
    page: Page, storage: Storage, monkeypatch
):
    """A DOM read exceeding its budget drops html/plaintext but keeps screenshot.

    CDP session creation, Page.stopLoading, and page.content share one
    wall-clock budget; a hang in any of them leaves the rendered DOM
    unavailable, but the screenshot (a separate stage) still captures.
    """
    fixture_html = (FIXTURES / "example.html").read_text()

    await page.route(
        "https://example.com",
        lambda route: route.fulfill(
            body=fixture_html,
            headers={"content-type": "text/html"},
        ),
    )

    # Tighten the DOM-capture budget and stall page.content past it.
    monkeypatch.setattr(capture_module, "DOM_CAPTURE_TIMEOUT_S", 0.2)

    async def slow_content():
        await asyncio.sleep(5)
        return fixture_html

    page.content = slow_content

    result = await capture_page(page, "https://example.com", storage)

    assert result.http_status == 200
    assert result.error is None
    # DOM capture timed out — rendered_html/plaintext unavailable.
    assert result.rendered_html is None
    assert result.plaintext is None
    # Screenshot is a separate stage and still captured.
    assert result.screenshot is not None


@pytest.mark.asyncio
async def test_capture_page_storage_write_timeout_skips_artifacts(
    page: Page, storage: Storage, monkeypatch
):
    """Artifact writes that exceed their budget yield None, not exceptions.

    Each storage write (rendered HTML, plaintext, screenshot) is bounded; a
    wedged backend cannot hang the capture. The page itself loaded, so that
    metadata survives — only the artifacts are dropped.
    """
    fixture_html = (FIXTURES / "example.html").read_text()

    await page.route(
        "https://example.com",
        lambda route: route.fulfill(
            body=fixture_html,
            headers={"content-type": "text/html"},
        ),
    )

    # Tighten the write budget and stall the storage backend past it. The real
    # put_blob path still runs; only the fsspec write boundary sleeps.
    monkeypatch.setattr(capture_module, "STORAGE_WRITE_TIMEOUT_S", 0.01)

    async def slow_pipe_file(path, value, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(storage.fs, "_pipe_file", slow_pipe_file)

    result = await capture_page(page, "https://example.com", storage)

    # The page committed and loaded; only the writes timed out.
    assert result.http_status == 200
    assert result.error is None
    assert result.final_url == "https://example.com/"
    assert result.plaintext is None
    assert result.rendered_html is None
    assert result.screenshot is None


@pytest.mark.asyncio
async def test_capture_page_storage_failure_skips_artifacts(
    page: Page, storage: Storage, monkeypatch
):
    """Artifact writes that fail operationally (not just on timeout) yield
    None, not exceptions.

    Each storage write (rendered HTML, plaintext, screenshot) is isolated; a
    backend that errors drops one artifact rather than the whole capture. The
    page itself loaded, so that metadata survives — only the artifacts are
    dropped.
    """
    fixture_html = (FIXTURES / "example.html").read_text()

    await page.route(
        "https://example.com",
        lambda route: route.fulfill(
            body=fixture_html,
            headers={"content-type": "text/html"},
        ),
    )

    # Make every storage write fail operationally. The real put_blob path still
    # runs; only the fsspec write boundary raises.
    async def failing_pipe_file(path, value, **kwargs):
        raise OSError("storage backend down")

    monkeypatch.setattr(storage.fs, "_pipe_file", failing_pipe_file)

    result = await capture_page(page, "https://example.com", storage)

    # The page committed and loaded; only the writes failed.
    assert result.http_status == 200
    assert result.error is None
    assert result.final_url == "https://example.com/"
    assert result.plaintext is None
    assert result.rendered_html is None
    assert result.screenshot is None


class _FakeDownload:
    """Minimal duck-typed stand-in for a Playwright ``Download``."""

    def __init__(self, save_error):
        self.url = "https://example.com/file.bin"
        self.suggested_filename = "file.bin"
        self._save_error = save_error

    async def save_as(self, path):
        if self._save_error is not None:
            raise self._save_error
        Path(path).write_bytes(b"data")


@pytest.mark.asyncio
async def test_save_download_failure_returns_none():
    """An operational failure saving a download yields None (leaving the HAR
    entry bodyless) instead of escaping and dropping the capture."""
    download = _FakeDownload(save_error=OSError("disk full"))
    result = await _save_download(download)
    assert result is None


@pytest.mark.asyncio
async def test_save_download_temp_directory_failure_returns_none(monkeypatch):
    """Failure to allocate the local download directory is also isolated."""

    def fail_mkdtemp():
        raise OSError("disk full")

    monkeypatch.setattr(capture_module.tempfile, "mkdtemp", fail_mkdtemp)

    result = await _save_download(_FakeDownload(save_error=None))

    assert result is None
