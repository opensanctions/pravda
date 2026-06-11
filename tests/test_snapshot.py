from pathlib import Path

import pytest
from playwright.async_api import async_playwright
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pravda.capture import capture_page
from pravda.db import Snapshot

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_capture_page_persists_snapshot(db_session):
    """Capture a page using a routed fixture, then verify DB records."""
    fixture_html = (FIXTURES / "example.html").read_text()

    async with async_playwright() as p:
        browser = await p.chromium.connect("ws://localhost:3000")
        context = await browser.new_context()
        page = await context.new_page()

        # Serve the fixture instead of making a real network request
        await page.route(
            "https://example.com",
            lambda route: route.fulfill(
                body=fixture_html,
                headers={"content-type": "text/html"},
            ),
        )

        snapshot = await capture_page(page, "https://example.com", db_session)

        await context.close()

    # Read back from the DB
    stmt = (
        select(Snapshot)
        .where(Snapshot.id == snapshot.id)
        .options(selectinload(Snapshot.contents), selectinload(Snapshot.headers))
    )
    result = await db_session.execute(stmt)
    loaded = result.scalar_one()

    assert str(loaded.id) == str(snapshot.id)
    assert loaded.url == "https://example.com"
    assert loaded.http_status == 200

    content_types = {c.content_type for c in loaded.contents}
    assert content_types == {
        "multipart/related",
        "image/png",
        "text/html",
        "text/plain",
    }

    # The MHTML content hash should correspond to the fixture HTML
    mhtml = next(c for c in loaded.contents if c.content_type == "multipart/related")
    assert len(mhtml.hash) == 64  # sha256 hex

    screenshot = next(c for c in loaded.contents if c.content_type == "image/png")
    assert len(screenshot.hash) == 64

    header_names = {h.name for h in loaded.headers}
    assert "content-type" in header_names
