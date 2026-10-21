"""End-to-end tests for ``Pravda.capture`` against the test browser."""

import pytest
from playwright.async_api import Error as PlaywrightError

from pravda import Pravda, derive_standalone_html
from tests.helpers import (
    PNG_BYTES,
    fulfill_html,
    fulfill_pdf,
    fulfill_standalone,
)


@pytest.mark.asyncio
async def test_capture_unreachable_url_returns_failed_snapshot(pravda: Pravda):
    """A default capture of an unreachable URL returns a failed snapshot."""
    snapshot = await pravda.capture("https://localhost:39999/")

    assert snapshot.url == "https://localhost:39999/"
    assert snapshot.http_status is None
    assert snapshot.error is not None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.screenshot is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_capture_drive_navigates_and_captures(pravda: Pravda, storage):
    """A drive callback routes and navigates; Pravda captures and returns."""

    async def drive(page, url):
        await page.route(url, fulfill_html)
        await page.goto(url, wait_until="load")

    snapshot = await pravda.capture("https://example.com", drive=drive)

    assert snapshot.url == "https://example.com"
    # goto normalizes to a trailing slash; final_url reflects that landing.
    assert snapshot.final_url == "https://example.com/"
    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.rendered_html.endswith(".html")
    assert snapshot.http_archive is not None
    html = (
        await storage.read_artifact(snapshot.rendered_html, snapshot.final_url)
    ).decode()
    assert "Hello from Pravda" in html


@pytest.mark.asyncio
async def test_derive_standalone_html_inlines_and_sanitizes(pravda: Pravda, storage):
    """derive_standalone_html() inlines and sanitizes the page's resources."""

    async def drive(page, url):
        await page.route(url, fulfill_standalone)
        await page.route(
            "https://example.com/style.css",
            lambda route: route.fulfill(
                body="body { background-color: teal; }",
                headers={"content-type": "text/css"},
            ),
        )
        await page.route(
            "https://example.com/logo.png",
            lambda route: route.fulfill(
                body=PNG_BYTES, headers={"content-type": "image/png"}
            ),
        )
        await page.route(
            "https://example.com/app.js",
            lambda route: route.fulfill(
                body="alert(1)", headers={"content-type": "text/javascript"}
            ),
        )
        await page.goto(url, wait_until="load")

    snapshot = await pravda.capture("https://example.com", drive=drive)

    assert snapshot.http_status == 200
    assert snapshot.error is None
    html = await derive_standalone_html(snapshot, storage)
    assert "Standalone fixture" in html
    assert "background-color: teal" in html
    assert "data:image/png;base64," in html
    assert "<script" not in html


@pytest.mark.asyncio
async def test_derive_standalone_html_requires_capture_evidence(
    pravda: Pravda, storage
):
    """derive_standalone_html() rejects snapshots without stored evidence."""

    snapshot = await pravda.capture("https://localhost:39999/")

    with pytest.raises(ValueError, match="evidence to derive standalone HTML"):
        await derive_standalone_html(snapshot, storage)


@pytest.mark.asyncio
async def test_capture_drive_interaction_and_readiness_captured(
    pravda: Pravda, storage
):
    """A drive callback's interactions and readiness checks are captured."""
    page_html = """
    <!DOCTYPE html>
    <html><body>
      <h1>Before interaction</h1>
      <button id="reveal">Reveal</button>
      <script>
        document.getElementById('reveal').addEventListener('click', () => {
          const node = document.createElement('p');
          node.id = 'secret';
          node.textContent = 'After interaction secret';
          document.body.appendChild(node);
        });
      </script>
    </body></html>
    """

    async def drive(page, url):
        await page.route(
            url,
            lambda route: route.fulfill(
                body=page_html, headers={"content-type": "text/html"}
            ),
        )
        await page.goto(url, wait_until="load")
        await page.click("#reveal")
        await page.wait_for_selector("#secret")

    snapshot = await pravda.capture("https://example.com", drive=drive)

    assert snapshot.http_status == 200
    assert snapshot.error is None
    html = (
        await storage.read_artifact(snapshot.rendered_html, snapshot.final_url)
    ).decode()
    assert "Before interaction" in html
    assert "After interaction secret" in html


@pytest.mark.asyncio
async def test_capture_drive_download_uses_download_url_and_skips_blank_page(
    pravda: Pravda,
):
    """A drive goto to a PDF records the download and skips the blank page artifacts."""

    async def drive(page, url):
        await page.route(url, fulfill_pdf)
        # goto hands off to Chrome's downloader and raises; catch it.
        try:
            await page.goto(url, wait_until="commit")
        except PlaywrightError:
            pass

    snapshot = await pravda.capture("https://example.com/doc.pdf", drive=drive)

    assert snapshot.url == "https://example.com/doc.pdf"
    assert snapshot.final_url == "https://example.com/doc.pdf"
    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.rendered_html is None
    assert snapshot.screenshot is None
    assert snapshot.http_archive is not None


@pytest.mark.asyncio
async def test_capture_drive_playwright_error_returns_failed_snapshot(
    pravda: Pravda,
):
    """A Playwright error from the drive callback returns a failed snapshot."""

    async def drive(page, url):
        await page.goto(url)

    snapshot = await pravda.capture("https://localhost:39999/", drive=drive)

    assert snapshot.url == "https://localhost:39999/"
    assert snapshot.http_status is None
    assert snapshot.error is not None
    assert snapshot.final_url is None
    assert snapshot.rendered_html is None
    assert snapshot.screenshot is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "data:text/html,<h1>hi</h1>",
        "about:blank",
        "file:///etc/hosts",
        "ftp://example.com/file",
        "https:///missing-host",
    ],
)
async def test_capture_rejects_non_http_url(pravda: Pravda, url: str):
    """A non-http(s) URL is rejected before any browser work."""
    with pytest.raises(ValueError, match="must be http"):
        await pravda.capture(url)


@pytest.mark.asyncio
async def test_capture_drive_rejects_non_http_final_url(pravda: Pravda):
    """A drive callback ending on a non-HTTP(S) URL raises ValueError."""

    async def drive(page, url):
        await page.goto("data:text/html,<h1>hi</h1>", wait_until="load")

    with pytest.raises(ValueError, match="non-HTTP"):
        await pravda.capture("https://example.com", drive=drive)


@pytest.mark.asyncio
async def test_capture_drive_requires_navigation(pravda: Pravda):
    async def drive(page, url):
        pass

    with pytest.raises(ValueError, match="before navigating"):
        await pravda.capture("https://example.com", drive=drive)


@pytest.mark.asyncio
async def test_capture_drive_arbitrary_error_propagates(pravda: Pravda):
    """A non-Playwright drive-callback exception propagates."""

    async def drive(page, url):
        await page.route(url, fulfill_html)
        await page.goto(url, wait_until="load")
        raise ValueError("caller bug")

    with pytest.raises(ValueError, match="caller bug"):
        await pravda.capture("https://example.com", drive=drive)


@pytest.mark.asyncio
async def test_capture_drive_iframe_navigation_does_not_overwrite_status(
    pravda: Pravda,
):
    """An iframe navigation response must not overwrite the main document's status."""
    main_html = """
    <!DOCTYPE html>
    <html><body>
      <h1>Main document</h1>
      <iframe id="frame"></iframe>
    </body></html>
    """

    async def drive(page, url):
        await page.route(
            url,
            lambda route: route.fulfill(
                body=main_html, headers={"content-type": "text/html"}
            ),
        )
        await page.route(
            "https://example.com/iframe",
            lambda route: route.fulfill(
                status=404, body="missing", headers={"content-type": "text/html"}
            ),
        )
        await page.goto(url, wait_until="load")
        # Fire the 404 navigation response while the drive observer listens.
        await page.eval_on_selector(
            "#frame", "el => el.src = 'https://example.com/iframe'"
        )
        await page.frame_locator("#frame").locator("body").wait_for()

    snapshot = await pravda.capture("https://example.com", drive=drive)

    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.final_url == "https://example.com/"
