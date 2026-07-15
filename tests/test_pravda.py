"""End-to-end tests for ``Pravda.snapshot`` against the browser and test database."""

from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

from pravda import Pravda

FIXTURES = Path(__file__).parent / "fixtures"

EXAMPLE_HTML = FIXTURES / "example.html"
SAMPLE_PDF = FIXTURES / "sample.pdf"


def _fulfill_html(route):
    return route.fulfill(
        body=EXAMPLE_HTML.read_text(), headers={"content-type": "text/html"}
    )


def _fulfill_pdf(route):
    return route.fulfill(
        body=SAMPLE_PDF.read_bytes(), headers={"content-type": "application/pdf"}
    )


@pytest.mark.asyncio
async def test_snapshot_persists_failed_attempt(pravda: Pravda):
    """A default capture of an unreachable URL persists a failed snapshot."""
    snapshot = await pravda.snapshot("https://localhost:39999/")

    assert snapshot.url == "https://localhost:39999/"
    assert snapshot.http_status is None
    assert snapshot.error is not None
    assert snapshot.final_url is None
    assert snapshot.plaintext is None
    assert snapshot.rendered_html is None
    assert snapshot.screenshot is None
    assert snapshot.http_archive is None

    history = await pravda.snapshots("https://localhost:39999/")
    assert any(item.id == snapshot.id for item in history)


@pytest.mark.asyncio
async def test_snapshot_drive_navigates_and_captures(pravda: Pravda):
    """A drive callback routes and navigates; Pravda captures and persists."""

    async def drive(page, url):
        await page.route(url, _fulfill_html)
        await page.goto(url, wait_until="load")

    snapshot = await pravda.snapshot("https://example.com", drive=drive)

    assert snapshot.url == "https://example.com"
    # goto normalizes to a trailing slash; final_url reflects that landing.
    assert snapshot.final_url == "https://example.com/"
    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.rendered_html.endswith(".html")
    assert snapshot.plaintext.endswith(".txt")
    assert snapshot.http_archive is not None
    text = Path(snapshot.plaintext).read_text()
    assert "Hello from Pravda" in text

    history = await pravda.snapshots("https://example.com")
    assert any(item.id == snapshot.id for item in history)


@pytest.mark.asyncio
async def test_snapshot_drive_interaction_and_readiness_captured(pravda: Pravda):
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

    snapshot = await pravda.snapshot("https://example.com", drive=drive)

    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.plaintext.endswith(".txt")
    text = Path(snapshot.plaintext).read_text()
    assert "Before interaction" in text
    assert "After interaction secret" in text


@pytest.mark.asyncio
async def test_snapshot_drive_download_uses_download_url_and_skips_blank_page(
    pravda: Pravda,
):
    """A drive goto to a PDF records the download and skips the blank page artifacts."""

    async def drive(page, url):
        await page.route(url, _fulfill_pdf)
        # goto hands off to Chrome's downloader and raises; catch it.
        try:
            await page.goto(url, wait_until="commit")
        except PlaywrightError:
            pass

    snapshot = await pravda.snapshot("https://example.com/doc.pdf", drive=drive)

    assert snapshot.url == "https://example.com/doc.pdf"
    assert snapshot.final_url == "https://example.com/doc.pdf"
    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.rendered_html is None
    assert snapshot.plaintext is None
    assert snapshot.screenshot is None
    assert snapshot.http_archive is not None


@pytest.mark.asyncio
async def test_snapshot_drive_playwright_error_persists_failed_attempt(pravda: Pravda):
    """A Playwright error from the drive callback persists as a failed attempt."""

    async def drive(page, url):
        await page.goto(url)

    snapshot = await pravda.snapshot("https://localhost:39999/", drive=drive)

    assert snapshot.url == "https://localhost:39999/"
    assert snapshot.http_status is None
    assert snapshot.error is not None
    assert snapshot.final_url is None
    assert snapshot.plaintext is None
    assert snapshot.rendered_html is None
    assert snapshot.screenshot is None
    assert snapshot.http_archive is None

    history = await pravda.snapshots("https://localhost:39999/")
    assert any(item.id == snapshot.id for item in history)


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
async def test_snapshot_rejects_non_http_url(pravda: Pravda, url: str):
    """A non-http(s) URL is rejected before any browser or database work."""
    with pytest.raises(ValueError, match="must be http"):
        await pravda.snapshot(url)

    assert await pravda.snapshots(url) == []


@pytest.mark.asyncio
async def test_snapshot_drive_rejects_non_http_final_url(pravda: Pravda):
    """A drive callback ending on a non-HTTP(S) URL raises ValueError."""

    async def drive(page, url):
        await page.goto("data:text/html,<h1>hi</h1>", wait_until="load")

    with pytest.raises(ValueError, match="non-HTTP"):
        await pravda.snapshot("https://example.com", drive=drive)

    assert await pravda.snapshots("https://example.com") == []


@pytest.mark.asyncio
async def test_snapshot_drive_requires_navigation(pravda: Pravda):
    async def drive(page, url):
        pass

    with pytest.raises(ValueError, match="before navigating"):
        await pravda.snapshot("https://example.com", drive=drive)

    assert await pravda.snapshots("https://example.com") == []


@pytest.mark.asyncio
async def test_snapshot_drive_arbitrary_error_propagates_and_persists_nothing(
    pravda: Pravda,
):
    """A non-Playwright drive-callback exception propagates and persists nothing."""

    async def drive(page, url):
        await page.route(url, _fulfill_html)
        await page.goto(url, wait_until="load")
        raise ValueError("caller bug")

    with pytest.raises(ValueError, match="caller bug"):
        await pravda.snapshot("https://example.com", drive=drive)

    assert await pravda.snapshots("https://example.com") == []


@pytest.mark.asyncio
async def test_snapshot_drive_iframe_navigation_does_not_overwrite_status(
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

    snapshot = await pravda.snapshot("https://example.com", drive=drive)

    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.final_url == "https://example.com/"
