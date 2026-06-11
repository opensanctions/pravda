import asyncio
import logging

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout
from sqlalchemy.ext.asyncio import AsyncSession

from pravda.db import ConditionType, Content, Header, Snapshot
from pravda.storage import put_blob

logger = logging.getLogger(__name__)

# Timeout for the page navigation itself.
NAV_TIMEOUT_MS = 10_000
# Timeout for capture operations after navigation (MHTML, screenshot, etc.).
# These can hang if the page is in a half-loaded state.
CAPTURE_TIMEOUT_MS = 15_000


async def capture_page(
    page: Page,
    url: str,
    session: AsyncSession,
    condition_type: ConditionType = ConditionType.lifecycle,
    condition: str = "load",
) -> Snapshot:
    """Navigate to *url*, capture evidence, store blobs, persist to *session*.

    Returns the ``Snapshot`` row (flushed, not committed — caller decides).
    """
    condition_met = True
    http_status: int | None = None
    resp_headers: dict[str, str] = {}
    error: str | None = None
    lifecycle_events: list[str] = []

    # --- CDP session: lifecycle tracking + MHTML capture ----------------
    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Page.enable", {})
    await cdp.send("Page.setLifecycleEventsEnabled", {"enabled": True})
    cdp.on(
        "Page.lifecycleEvent",
        lambda params: lifecycle_events.append(params["name"]),
    )

    # --- Navigation ------------------------------------------------------
    try:
        if condition_type is ConditionType.lifecycle:
            response = await page.goto(
                url, wait_until=condition, timeout=NAV_TIMEOUT_MS
            )
        else:
            response = await page.goto(url, timeout=NAV_TIMEOUT_MS)
            await page.wait_for_selector(condition, timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeout as exc:
        logger.warning(
            "Timeout waiting for %s (condition_type=%s, condition=%s)",
            url,
            condition_type.value,
            condition,
        )
        condition_met = False
        error = str(exc)
        response = None  # type: ignore[assignment]

    # Grab HTTP response (available even when load/selector times out).
    if response is not None:
        http_status = response.status
        raw = await response.all_headers()
        resp_headers = {k.lower(): v for k, v in raw.items()}

    logger.info("Lifecycle events for %s: %s", url, lifecycle_events)

    # --- Capture page content -------------------------------------------
    # If the condition timed out, only capture when the page reached at
    # least DOMContentLoaded — otherwise there is no meaningful content.
    should_capture = condition_met or "DOMContentLoaded" in lifecycle_events

    mhtml_bytes: bytes | None = None
    screenshot_bytes: bytes | None = None
    rendered_html: bytes | None = None
    inner_text: bytes | None = None

    if should_capture:
        try:
            mhtml_response = await asyncio.wait_for(
                cdp.send("Page.captureSnapshot", {"format": "mhtml"}),
                timeout=CAPTURE_TIMEOUT_MS / 1000,
            )
            mhtml_bytes = mhtml_response["data"].encode("utf-8")
        except (PlaywrightTimeout, asyncio.TimeoutError):
            logger.warning("Timeout capturing MHTML for %s", url)

        try:
            screenshot_bytes = await page.screenshot(
                full_page=True, timeout=CAPTURE_TIMEOUT_MS
            )
        except PlaywrightTimeout:
            logger.warning("Timeout capturing screenshot for %s", url)

        try:
            rendered_html = await asyncio.wait_for(
                page.content(), timeout=CAPTURE_TIMEOUT_MS / 1000
            )
            rendered_html = rendered_html.encode("utf-8")
        except (PlaywrightTimeout, asyncio.TimeoutError):
            logger.warning("Timeout capturing rendered HTML for %s", url)

        try:
            inner_text = (
                await page.inner_text("body", timeout=CAPTURE_TIMEOUT_MS)
            ).encode("utf-8")
        except PlaywrightTimeout:
            logger.warning("Timeout capturing inner text for %s", url)
    else:
        logger.warning(
            "Skipping captures for %s — page never reached DOMContentLoaded",
            url,
        )

    # --- Store blobs ----------------------------------------------------
    contents: list[Content] = []
    if mhtml_bytes is not None:
        contents.append(
            Content(
                content_type="multipart/related",
                hash=await put_blob(mhtml_bytes),
            )
        )
    if screenshot_bytes is not None:
        contents.append(
            Content(
                content_type="image/png",
                hash=await put_blob(screenshot_bytes),
            )
        )
    if rendered_html is not None:
        contents.append(
            Content(
                content_type="text/html",
                hash=await put_blob(rendered_html),
            )
        )
    if inner_text is not None:
        contents.append(
            Content(
                content_type="text/plain",
                hash=await put_blob(inner_text),
            )
        )

    # --- Persist snapshot row -------------------------------------------
    snapshot = Snapshot(
        url=url,
        http_status=http_status,
        error=error,
        condition_type=condition_type,
        condition=condition,
        condition_met=condition_met,
        lifecycle_events=lifecycle_events,
    )
    snapshot.contents = contents
    snapshot.headers = [
        Header(name=name, value=value) for name, value in resp_headers.items()
    ]
    session.add(snapshot)
    await session.flush()
    logger.info("Saved snapshot %s for %s", snapshot.id, url)

    return snapshot
