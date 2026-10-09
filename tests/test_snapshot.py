import asyncio
from pathlib import Path

import pytest
from playwright.async_api import Browser, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

import pravda.capture as capture_module
from pravda.capture import _save_download, capture_page
from tests.helpers import FIXTURES, fulfill_html, fulfill_pdf


@pytest.fixture()
async def page(browser: Browser):
    context = await browser.new_context()
    page = await context.new_page()
    try:
        yield page
    finally:
        await context.close()


@pytest.mark.asyncio
async def test_capture_page_returns_evidence(page: Page):
    """Capture a page using a routed fixture and inspect the evidence."""
    await page.route("https://example.com", fulfill_html)

    result = await capture_page(page, "https://example.com")

    assert result.http_status == 200
    assert result.error is None
    assert result.final_url == "https://example.com/"

    assert "Hello from Pravda" in result.html
    assert result.screenshot.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_capture_page_downloads_pdf(page: Page):
    """A URL serving application/pdf is captured as a downloaded body."""
    await page.route("https://example.com/doc.pdf", fulfill_pdf)

    result = await capture_page(page, "https://example.com/doc.pdf")

    assert result.http_status == 200
    assert result.error is None
    assert result.final_url == "https://example.com/doc.pdf"

    assert result.download is not None
    assert result.download.url == "https://example.com/doc.pdf"
    assert result.download.data == (FIXTURES / "sample.pdf").read_bytes()
    assert result.html is None
    assert result.screenshot is None


@pytest.mark.asyncio
async def test_capture_page_goto_timeout_skips_captures(page: Page):
    """A navigation that never commits skips captures entirely."""

    async def fake_goto(*args, **kwargs):
        raise PlaywrightTimeout("Navigation timeout")

    page.goto = fake_goto

    result = await capture_page(page, "https://timeout.example.com")

    assert result.http_status is None
    assert result.error is not None
    assert result.final_url is None

    assert result.html is None
    assert result.screenshot is None


@pytest.mark.asyncio
async def test_http_commit_captured_when_load_times_out(page: Page, monkeypatch):
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
    result = await capture_page(page, "https://slow.example.com")

    assert result.http_status == 200
    assert result.final_url == "https://slow.example.com/"

    assert result.error is not None

    assert result.html is not None
    assert result.screenshot is not None


@pytest.mark.asyncio
async def test_capture_page_dom_capture_timeout_skips_content(page: Page, monkeypatch):
    """A DOM read exceeding its budget drops html but keeps the screenshot."""
    fixture_html = (FIXTURES / "example.html").read_text()
    await page.route("https://example.com", fulfill_html)

    # Stall page.content past the DOM-capture budget.
    monkeypatch.setattr(capture_module, "DOM_CAPTURE_TIMEOUT_S", 0.2)

    async def slow_content():
        await asyncio.sleep(5)
        return fixture_html

    page.content = slow_content

    result = await capture_page(page, "https://example.com")

    assert result.http_status == 200
    assert result.error is None
    assert result.html is None
    assert result.screenshot is not None


@pytest.mark.asyncio
async def test_capture_page_waits_for_dom_settle(page: Page):
    """Late DOM re-renders land in the captured HTML instead of the initial state."""
    await page.route(
        "https://example.com",
        lambda route: route.fulfill(
            body=(FIXTURES / "hydrating.html").read_text(),
            headers={"content-type": "text/html"},
        ),
    )

    result = await capture_page(page, "https://example.com")

    assert result.http_status == 200
    assert "final" in result.html
    assert ">loading<" not in result.html
    assert ">intermediate<" not in result.html
    assert ">still-hydrating<" not in result.html


@pytest.mark.asyncio
async def test_capture_page_never_settling_dom_drops_html(page: Page, monkeypatch):
    """A DOM that never settles hits the capture budget and drops only html."""
    await page.route(
        "https://example.com",
        lambda route: route.fulfill(
            body=(FIXTURES / "ticker.html").read_text(),
            headers={"content-type": "text/html"},
        ),
    )
    monkeypatch.setattr(capture_module, "DOM_CAPTURE_TIMEOUT_S", 1.5)

    result = await capture_page(page, "https://example.com")

    assert result.http_status == 200
    assert result.error is None
    assert result.html is None
    assert result.screenshot is not None


@pytest.mark.asyncio
async def test_capture_page_hanging_font_drops_screenshot(page: Page, monkeypatch):
    """A font that never loads stalls only the screenshot, not the html."""
    await page.route(
        "https://example.com/hanging-font.woff2",
        lambda route: asyncio.Event().wait(),  # never resolves
    )
    await page.route(
        "https://example.com",
        lambda route: route.fulfill(
            body=(FIXTURES / "font-stall.html").read_text(),
            headers={"content-type": "text/html"},
        ),
    )
    monkeypatch.setattr(capture_module, "LOAD_TIMEOUT_MS", 2000)
    monkeypatch.setattr(capture_module, "CAPTURE_TIMEOUT_MS", 1500)

    result = await capture_page(page, "https://example.com")

    assert result.http_status == 200
    # The hanging font keeps the load event from firing, and Playwright's
    # screenshot stalls on document.fonts.ready: both degrade gracefully.
    assert result.error is not None
    assert result.html is not None
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
async def test_save_download_failure_propagates():
    download = _FakeDownload(save_error=OSError("disk full"))
    with pytest.raises(OSError, match="disk full"):
        await _save_download(download)
