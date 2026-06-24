import asyncio
import logging
from dataclasses import dataclass

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from pravda.db import ConditionType
from pravda.storage import put_blob

logger = logging.getLogger(__name__)

# Timeout for navigation (reaching "commit" — first HTTP response received).
NAV_TIMEOUT_MS = 10_000

# Timeout for waiting on the page condition after navigation.
CONDITION_TIMEOUT_MS = 30_000

# Timeout for each individual capture operation (MHTML, screenshot, etc.).
CAPTURE_TIMEOUT_MS = 15_000


@dataclass
class CaptureResult:
    """Pure evidence captured from a page — no persistence concerns."""

    http_status: int | None
    error: str | None
    condition_met: bool
    headers: dict[str, str]
    final_url: str | None
    plaintext_hash: str | None
    rendered_html_hash: str | None
    screenshot_hash: str | None
    blob_hash: str | None
    blob_content_type: str | None


async def capture_page(
    page: Page,
    url: str,
    condition_type: ConditionType = ConditionType.lifecycle,
    condition: str = "load",
    condition_timeout_ms: int = CONDITION_TIMEOUT_MS,
) -> CaptureResult:
    """Navigate to *url* and capture evidence: HTTP response and
    MHTML/screenshot/HTML/text blobs.

    Returns the evidence as a ``CaptureResult``. Storing it is the caller's
    job — this function never touches the database.
    """
    navigation = await _navigate(
        page, url, condition_type, condition, condition_timeout_ms
    )

    if navigation.http_status is None:
        # Navigation never committed — there is nothing on the page to capture.
        artifacts = CapturedArtifacts(None, None, None, None, None)
    else:
        artifacts = await _capture_artifacts(page, navigation.final_url)

    return CaptureResult(
        http_status=navigation.http_status,
        error=navigation.error,
        condition_met=navigation.condition_met,
        headers=navigation.headers,
        final_url=navigation.final_url,
        plaintext_hash=artifacts.plaintext_hash,
        rendered_html_hash=artifacts.rendered_html_hash,
        screenshot_hash=artifacts.screenshot_hash,
        blob_hash=artifacts.blob_hash,
        blob_content_type=artifacts.blob_content_type,
    )


@dataclass
class _Navigation:
    http_status: int | None
    headers: dict[str, str]
    condition_met: bool
    error: str | None
    final_url: str | None


async def _navigate(
    page: Page,
    url: str,
    condition_type: ConditionType,
    condition: str,
    condition_timeout_ms: int,
) -> _Navigation:
    """Navigate to *url*, then wait for the requested condition.

    Status and headers are read at "commit" (first response), *before* the
    condition wait — so a condition timeout still records the HTTP response.

    Lifecycle conditions use Playwright's ``wait_for_load_state`` ("commit"
    needs no extra wait — ``page.goto`` already waited for it); selector
    conditions use ``wait_for_selector``.
    """
    http_status: int | None = None
    headers: dict[str, str] = {}
    final_url: str | None = None
    try:
        response = await page.goto(url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        http_status = response.status
        headers = {
            key.lower(): value for key, value in (await response.all_headers()).items()
        }
        # page.url reflects any redirects that happened during navigation.
        final_url = page.url

        if condition_type is ConditionType.selector:
            await page.wait_for_selector(condition, timeout=condition_timeout_ms)
        elif condition != "commit":
            await page.wait_for_load_state(condition, timeout=condition_timeout_ms)

        return _Navigation(
            http_status,
            headers,
            condition_met=True,
            error=None,
            final_url=final_url,
        )
    except (PlaywrightTimeout, asyncio.TimeoutError) as exception:
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
            http_status, headers, condition_met=False, error=error, final_url=final_url
        )


@dataclass
class CapturedArtifacts:
    """Hashes of the four captured artifacts, plus the blob's MIME type.

    Three are fixed-MIME (plaintext/rendered_html/screenshot); the blob is
    polymorphic (multipart/related today, application/pdf and others later),
    so its content type is recorded alongside.
    """

    plaintext_hash: str | None
    rendered_html_hash: str | None
    screenshot_hash: str | None
    blob_hash: str | None
    blob_content_type: str | None


async def _capture_artifacts(page: Page, url: str) -> CapturedArtifacts:
    """Stop any pending requests, then capture the four artifacts.

    Stopping the page first forces it into a terminal, capturable state —
    otherwise screenshot/MHTML could stall on resources that never arrive.
    This mirrors hitting the browser's stop button.
    """
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Page.stopLoading", {})

    async def capture_mhtml() -> str:
        result = await asyncio.wait_for(
            cdp.send("Page.captureSnapshot", {"format": "mhtml"}),
            CAPTURE_TIMEOUT_MS / 1000,
        )
        return result["data"]

    plaintext_hash = await _capture_one(
        "plaintext",
        lambda: page.inner_text("body", timeout=CAPTURE_TIMEOUT_MS),
        url,
    )
    rendered_html_hash = await _capture_one(
        "rendered_html",
        lambda: page.content(),
        url,
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
    screenshot_hash = await _capture_one(
        "screenshot",
        lambda: page.screenshot(
            full_page=True,
            clip=screenshot_clip,
            timeout=CAPTURE_TIMEOUT_MS,
        ),
        url,
    )
    blob_hash = await _capture_one("blob", capture_mhtml, url)

    return CapturedArtifacts(
        plaintext_hash=plaintext_hash,
        rendered_html_hash=rendered_html_hash,
        screenshot_hash=screenshot_hash,
        blob_hash=blob_hash,
        blob_content_type="multipart/related" if blob_hash is not None else None,
    )


async def _capture_one(name: str, callback, url: str) -> str | None:
    """Capture one artifact via *callback* and store the blob."""
    try:
        data = await callback()
        if isinstance(data, str):
            data = data.encode()
        return await put_blob(data, url)
    except (asyncio.TimeoutError, PlaywrightTimeout):
        logger.warning("Timeout capturing %s for %s", name, url)
        return None
    except Exception as exception:
        logger.warning("Failed to capture %s for %s: %s", name, url, exception)
        return None
