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

    # The HAR is a context-lifecycle concern, so capture_page does not touch it.
    assert result.plaintext.endswith(".txt")
    assert result.rendered_html.endswith(".html")
    assert result.screenshot.endswith(".png")


@pytest.mark.asyncio
async def test_capture_page_downloads_pdf(page: Page, storage: Storage):
    """A URL serving application/pdf is captured as a downloaded body."""
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

    assert result.download is not None
    assert result.download.url == "https://example.com/doc.pdf"
    assert result.download.data == fixture_pdf
    assert result.plaintext is None
    assert result.rendered_html is None
    assert result.screenshot is None


@pytest.mark.asyncio
async def test_capture_page_goto_timeout_skips_captures(page: Page, storage: Storage):
    """A navigation that never commits skips captures entirely."""

    async def fake_goto(*args, **kwargs):
        raise PlaywrightTimeout("Navigation timeout")

    page.goto = fake_goto

    result = await capture_page(page, "https://timeout.example.com", storage)

    assert result.http_status is None
    assert result.error is not None
    assert result.final_url is None

    assert result.plaintext is None
    assert result.rendered_html is None
    assert result.screenshot is None


@pytest.mark.asyncio
async def test_http_commit_captured_when_load_times_out(
    page: Page, storage: Storage, monkeypatch
):
    """HTTP status comes from commit even when load times out."""
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

    monkeypatch.setattr(capture_module, "LOAD_TIMEOUT_MS", 2000)
    result = await capture_page(page, "https://slow.example.com", storage)

    assert result.http_status == 200
    assert result.final_url == "https://slow.example.com/"

    assert result.error is not None

    assert result.plaintext is not None
    assert result.rendered_html is not None
    assert result.screenshot is not None


@pytest.mark.asyncio
async def test_capture_page_dom_capture_timeout_skips_content(
    page: Page, storage: Storage, monkeypatch
):
    """A DOM read exceeding its budget drops html/plaintext but keeps the screenshot."""
    fixture_html = (FIXTURES / "example.html").read_text()

    await page.route(
        "https://example.com",
        lambda route: route.fulfill(
            body=fixture_html,
            headers={"content-type": "text/html"},
        ),
    )

    # Stall page.content past the DOM-capture budget.
    monkeypatch.setattr(capture_module, "DOM_CAPTURE_TIMEOUT_S", 0.2)

    async def slow_content():
        await asyncio.sleep(5)
        return fixture_html

    page.content = slow_content

    result = await capture_page(page, "https://example.com", storage)

    assert result.http_status == 200
    assert result.error is None
    assert result.rendered_html is None
    assert result.plaintext is None
    assert result.screenshot is not None


@pytest.mark.asyncio
async def test_capture_page_storage_write_timeout_skips_artifacts(
    page: Page, storage: Storage, monkeypatch
):
    """Artifact writes that exceed their budget yield None, not exceptions."""
    fixture_html = (FIXTURES / "example.html").read_text()

    await page.route(
        "https://example.com",
        lambda route: route.fulfill(
            body=fixture_html,
            headers={"content-type": "text/html"},
        ),
    )

    # Stall the storage backend past the write budget.
    monkeypatch.setattr(capture_module, "STORAGE_WRITE_TIMEOUT_S", 0.01)

    async def slow_pipe_file(path, value, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(storage.fs, "_pipe_file", slow_pipe_file)

    result = await capture_page(page, "https://example.com", storage)

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
    """Artifact writes that fail operationally yield None, not exceptions."""
    fixture_html = (FIXTURES / "example.html").read_text()

    await page.route(
        "https://example.com",
        lambda route: route.fulfill(
            body=fixture_html,
            headers={"content-type": "text/html"},
        ),
    )

    # Make every storage write fail operationally at the fsspec boundary.
    async def failing_pipe_file(path, value, **kwargs):
        raise OSError("storage backend down")

    monkeypatch.setattr(storage.fs, "_pipe_file", failing_pipe_file)

    result = await capture_page(page, "https://example.com", storage)

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
    """An operational failure saving a download yields None instead of escaping."""
    download = _FakeDownload(save_error=OSError("disk full"))
    result = await _save_download(download)
    assert result is None
