import asyncio
import logging
from dataclasses import dataclass

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from pravda.db import ConditionType
from pravda.storage import content_hash, put_blob

logger = logging.getLogger(__name__)

# Timeout for navigation (reaching "commit" — first HTTP response received).
NAV_TIMEOUT_MS = 10_000

# Timeout for waiting on the page condition after navigation.
CONDITION_TIMEOUT_MS = 30_000

# Timeout for each individual capture operation (screenshot, etc.).
CAPTURE_TIMEOUT_MS = 15_000


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

    Returns the evidence as a ``CaptureResult``. Storing it is the caller's
    job — this function never touches the database.
    """
    navigation = await _navigate(
        page, url, condition_type, condition, condition_timeout_ms
    )

    if navigation.http_status is None:
        # Navigation never committed — there is nothing on the page to capture.
        artifacts = CapturedArtifacts(None, None, None)
    else:
        artifacts = await _capture_artifacts(page, navigation.final_url)

    return CaptureResult(
        http_status=navigation.http_status,
        error=navigation.error,
        condition_met=navigation.condition_met,
        final_url=navigation.final_url,
        plaintext=artifacts.plaintext,
        rendered_html=artifacts.rendered_html,
        screenshot=artifacts.screenshot,
    )


@dataclass
class _Navigation:
    http_status: int | None
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

    Status is read at "commit" (first response), *before* the condition
    wait — so a condition timeout still records the HTTP response. Response
    headers live in the HAR recording, so they are not captured here.

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
            http_status, condition_met=False, error=error, final_url=final_url
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
        name = f"{content_hash(data)}.{extension}"
        return await put_blob(name, data, url)
    except (asyncio.TimeoutError, PlaywrightTimeout):
        logger.warning("Timeout capturing %s for %s", name, url)
        return None
    except Exception as exception:
        logger.warning("Failed to capture %s for %s: %s", name, url, exception)
        return None
