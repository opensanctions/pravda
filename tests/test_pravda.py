"""End-to-end tests for the configured Pravda instance's capture path.

These exercise ``Pravda.snapshot`` against the real browser server and test
database: the default (no ``drive``) behavior and the ``drive`` callback path
where the caller pilots the recording page. Routed pages serve fixture
content without real network access; fixtures isolate the test database and
artifact store between cases.
"""

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
    """A default (no ``drive``) capture of an unreachable URL persists a failed
    Snapshot row.

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

    # Committed through Pravda's own session — visible to snapshots().
    history = await pravda.snapshots("https://localhost:39999/")
    assert any(item.id == snapshot.id for item in history)


@pytest.mark.asyncio
async def test_snapshot_drive_navigates_and_captures(pravda: Pravda):
    """A drive callback that routes and navigates leaves Pravda to capture and
    persist the resulting page, including the recorded HAR.

    The callback owns navigation (complete control, including the initial
    goto); Pravda captures whatever state it leaves behind. The recorded url
    is the subject the caller asked to capture; final_url is where the page
    landed.
    """

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
    # The captured plaintext is the fixture's content.
    text = (Path(snapshot.prefix) / snapshot.plaintext).read_text()
    assert "Hello from Pravda" in text

    # Committed through Pravda's own session — visible to snapshots().
    history = await pravda.snapshots("https://example.com")
    assert any(item.id == snapshot.id for item in history)


@pytest.mark.asyncio
async def test_snapshot_drive_interaction_and_readiness_captured(pravda: Pravda):
    """A drive callback's interactions and readiness checks are part of the
    captured evidence.

    The callback routes a page whose button appends a node via JS, clicks it,
    and waits for the new selector (readiness) before returning. The snapshot's
    plaintext reflects the post-click DOM — proving callback-controlled
    interaction, not just navigation, was captured.
    """
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
    # The post-click DOM (appended by the callback's interaction) was captured.
    text = (Path(snapshot.prefix) / snapshot.plaintext).read_text()
    assert "Before interaction" in text
    assert "After interaction secret" in text


@pytest.mark.asyncio
async def test_snapshot_drive_download_uses_download_url_and_skips_blank_page(
    pravda: Pravda,
):
    """A drive callback that navigates to a PDF (handed off to Chrome's
    downloader) records the download's URL and skips the meaningless
    about:blank artifacts, matching the default PDF path.

    The callback owns the goto and catches the ``Download is starting`` error
    it raises; Pravda observes the response status and the download event, then
    recovers and folds the bytes back into the recorded HAR.
    """

    async def drive(page, url):
        await page.route(url, _fulfill_pdf)
        # goto hands off to Chrome's downloader and raises "Download is
        # starting"; the drive-capture observer records the response status
        # and the download.
        try:
            await page.goto(url, wait_until="commit")
        except PlaywrightError:
            pass

    snapshot = await pravda.snapshot("https://example.com/doc.pdf", drive=drive)

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
async def test_snapshot_drive_playwright_error_persists_failed_attempt(pravda: Pravda):
    """A Playwright error raised from the drive callback follows snapshot's
    established failed-attempt persistence: a Snapshot row with the error and
    no evidence (the navigation failed fast against a closed local port)."""

    async def drive(page, url):
        # Nothing listens here, so goto fails fast with a Playwright error.
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
    """A requested URL that is not http(s) is rejected before any browser or
    database work: it raises ValueError and persists nothing."""
    with pytest.raises(ValueError, match="must be http"):
        await pravda.snapshot(url)

    assert await pravda.snapshots(url) == []


@pytest.mark.asyncio
async def test_snapshot_drive_rejects_non_http_final_url(pravda: Pravda):
    """A drive callback that ends on a non-HTTP(S) URL (and captured no
    download) is callback misuse: it raises ValueError and persists nothing —
    a deliberate about:/data:/file:/blob: final state is not capturable
    evidence."""

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
    """A non-Playwright exception raised by the drive callback is not turned
    into a snapshot failure: it propagates to the caller and persists nothing."""

    async def drive(page, url):
        await page.route(url, _fulfill_html)
        await page.goto(url, wait_until="load")
        raise ValueError("caller bug")

    with pytest.raises(ValueError, match="caller bug"):
        await pravda.snapshot("https://example.com", drive=drive)

    # Nothing was persisted — the exception escaped before persistence.
    assert await pravda.snapshots("https://example.com") == []


@pytest.mark.asyncio
async def test_snapshot_drive_iframe_navigation_does_not_overwrite_status(
    pravda: Pravda,
):
    """A navigation response fired by an iframe during a drive session must
    not overwrite the main document's HTTP status.

    The shared page observer records only main-frame navigation responses, so
    even though the iframe's document load here returns 404, the snapshot's
    status stays the main document's 200. This is the regression for the
    consolidated download/response tracking: an iframe navigation can never
    overwrite the subject HTTP status, in either the default or the driven
    path.
    """
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
        # Navigate the iframe and wait for its document to load, so the 404
        # navigation response fires while the drive observer is listening.
        await page.eval_on_selector(
            "#frame", "el => el.src = 'https://example.com/iframe'"
        )
        await page.frame_locator("#frame").locator("body").wait_for()

    snapshot = await pravda.snapshot("https://example.com", drive=drive)

    # The main document's 200 stands; the iframe's 404 did not overwrite it.
    assert snapshot.http_status == 200
    assert snapshot.error is None
    assert snapshot.final_url == "https://example.com/"
