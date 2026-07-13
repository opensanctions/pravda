"""Direct library tests for the one-shot and interactive capture paths.

These exercise ``pravda.snapshot`` and ``pravda.browser()`` end to end against
the real browser server and test database. Routed pages serve fixture content
without real network access, and committed rows and artifacts are cleaned up
by test fixtures.
"""

from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

import pravda
from pravda.snapshots import snapshots

FIXTURES = Path(__file__).parent / "fixtures"

EXAMPLE_HTML = FIXTURES / "example.html"
SAMPLE_PDF = FIXTURES / "sample.pdf"

pytestmark = pytest.mark.usefixtures("clean_snapshots", "storage_tmp")


def _fulfill_html(route):
    return route.fulfill(
        body=EXAMPLE_HTML.read_text(), headers={"content-type": "text/html"}
    )


def _fulfill_pdf(route):
    return route.fulfill(
        body=SAMPLE_PDF.read_bytes(), headers={"content-type": "application/pdf"}
    )


@pytest.mark.asyncio
async def test_snapshot_persists_failed_attempt():
    """A one-shot capture of an unreachable URL persists a failed Snapshot row.

    Drives the full pipeline (connect, context, navigate, finalize, persist)
    against the real browser and test database. Nothing listens on the local
    port, so the navigation fails fast with no network egress and the attempt
    is persisted with an error and no evidence.
    """
    snapshot = await pravda.snapshot("https://localhost:39999/")

    assert snapshot.url == "https://localhost:39999/"
    assert snapshot.http_status is None
    assert snapshot.error is not None
    assert snapshot.final_url is None
    assert snapshot.plaintext is None
    assert snapshot.rendered_html is None
    assert snapshot.screenshot is None
    assert snapshot.http_archive is None

    # Committed through Pravda's own session — visible to the history API.
    history = await snapshots("https://localhost:39999/")
    assert any(item.id == snapshot.id for item in history)


@pytest.mark.asyncio
async def test_interactive_capture_after_navigation():
    """An interactive browser() session captures and persists the page the
    caller navigated to, including the recorded HAR."""
    async with pravda.browser() as session:
        page = session.page
        await page.route("https://example.com", _fulfill_html)
        await page.goto("https://example.com", wait_until="load")

        snapshot = await session.snapshot()

    assert snapshot.url == "https://example.com/"
    assert snapshot.final_url == "https://example.com/"
    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.rendered_html.endswith(".html")
    assert snapshot.plaintext.endswith(".txt")
    assert snapshot.http_archive is not None


@pytest.mark.asyncio
async def test_interactive_download_uses_download_url_and_skips_blank_page():
    """A navigation that hands off to a download records the download's URL and
    skips the meaningless about:blank artifacts, matching the one-shot PDF
    path."""
    async with pravda.browser() as session:
        page = session.page
        await page.route("https://example.com/doc.pdf", _fulfill_pdf)
        # goto hands off to Chrome's downloader and raises "Download is
        # starting"; the session observes the response status and download.
        try:
            await page.goto("https://example.com/doc.pdf", wait_until="commit")
        except PlaywrightError:
            pass

        snapshot = await session.snapshot()

    assert snapshot.url == "https://example.com/doc.pdf"
    assert snapshot.final_url == "https://example.com/doc.pdf"
    assert snapshot.http_status == 200
    assert snapshot.error is None
    # The page held about:blank — nothing meaningful to capture.
    assert snapshot.rendered_html is None
    assert snapshot.plaintext is None
    assert snapshot.screenshot is None
    # The download body is folded back into the recorded HAR.
    assert snapshot.http_archive is not None


@pytest.mark.asyncio
async def test_interactive_snapshot_is_terminal():
    """After snapshot(), .page and a second snapshot() raise PravdaError, and
    exiting the context manager afterwards is safe (idempotent cleanup)."""
    async with pravda.browser() as session:
        page = session.page
        await page.route("https://example.com", _fulfill_html)
        await page.goto("https://example.com")
        first = await session.snapshot()

        # .page is gone once the session is terminal.
        with pytest.raises(pravda.PravdaError):
            _ = session.page
        # A second snapshot() is refused.
        with pytest.raises(pravda.PravdaError):
            await session.snapshot()

    # Exiting the context after a terminal snapshot does not raise.
    assert first.http_status == 200


@pytest.mark.asyncio
async def test_interactive_snapshot_before_navigation_errors_and_persists_nothing():
    """Calling snapshot() before any navigation raises PravdaError and persists
    nothing — no bogus about:blank "success" row."""
    async with pravda.browser() as session:
        _ = session.page  # a page exists, but it was never navigated

        with pytest.raises(pravda.PravdaError, match="before any navigation"):
            await session.snapshot()

    # Nothing was persisted.
    assert await snapshots("about:blank") == []
