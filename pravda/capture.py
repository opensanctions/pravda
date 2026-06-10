import logging

from playwright.async_api import Page
from sqlalchemy.ext.asyncio import AsyncSession

from pravda.db import Content, Header, Snapshot
from pravda.storage import put_blob

logger = logging.getLogger(__name__)


async def capture_page(page: Page, url: str, session: AsyncSession) -> Snapshot:
    """Navigate to *url*, capture evidence, store blobs, persist to *session*.

    Returns the ``Snapshot`` row (flushed, not committed — caller decides).
    """
    response = await page.goto(url, wait_until="load")
    http_status = response.status if response else 0

    # Collect response headers
    resp_headers: dict[str, str] = {}
    if response:
        raw = await response.all_headers()
        resp_headers = {k.lower(): v for k, v in raw.items()}

    cdp = await page.context.new_cdp_session(page)
    mhtml_response = await cdp.send("Page.captureSnapshot", {"format": "mhtml"})
    mhtml_bytes = mhtml_response["data"].encode("utf-8")
    await cdp.detach()

    screenshot_bytes = await page.screenshot(full_page=True)

    # Store blobs
    mhtml_hash = await put_blob(mhtml_bytes)
    screenshot_hash = await put_blob(screenshot_bytes)

    # Persist snapshot row
    snapshot = Snapshot(url=url, http_status=http_status)
    session.add(snapshot)
    await session.flush()

    session.add(
        Content(
            snapshot_id=snapshot.id, content_type="multipart/related", hash=mhtml_hash
        )
    )
    session.add(
        Content(
            snapshot_id=snapshot.id,
            content_type="image/png",
            hash=screenshot_hash,
        )
    )

    for name, value in resp_headers.items():
        session.add(Header(snapshot_id=snapshot.id, name=name, value=value))

    await session.flush()
    logger.info("Saved snapshot %s for %s", snapshot.id, url)

    return snapshot
