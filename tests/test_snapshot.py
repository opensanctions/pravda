import asyncio
from pathlib import Path

import pytest
from playwright.async_api import Browser
from playwright.async_api import TimeoutError as PlaywrightTimeout
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pravda.capture import capture_page
from pravda.db import ConditionType, Snapshot

FIXTURES = Path(__file__).parent / "fixtures"

# HTML that blocks `load` by holding open a never-resolving resource.
# DOMContentLoaded fires immediately (DOM is fully parsed), but the
# browser waits forever for the image — perfect for testing the
# "DOMContentLoaded fires, load never does" scenario.
BLOCK_LOAD_HTML = """\
<!DOCTYPE html>
<html><head><title>Blocking</title></head>
<body><h1>Load is blocked</h1><img src="/slow-resource.png"></body>
</html>
"""


@pytest.mark.asyncio
async def test_capture_page_persists_snapshot(db_session, browser: Browser):
    """Capture a page using a routed fixture, then verify DB records."""
    fixture_html = (FIXTURES / "example.html").read_text()

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
    assert loaded.condition_type == ConditionType.lifecycle
    assert loaded.condition == "load"
    assert loaded.condition_met is True

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


@pytest.mark.asyncio
async def test_capture_page_timeout_no_lifecycle_skips_captures(
    db_session, browser: Browser
):
    """A timeout before any lifecycle event fires skips captures entirely."""

    context = await browser.new_context()
    page = await context.new_page()

    # Mock goto to raise timeout immediately — no real waiting,
    # no lifecycle events fire.
    async def fake_goto(*args, **kwargs):
        raise PlaywrightTimeout("Navigation timeout")

    page.goto = fake_goto

    snapshot = await capture_page(
        page, "https://timeout.example.com", db_session, condition="load"
    )

    await context.close()

    # Read back from the DB
    stmt = (
        select(Snapshot)
        .where(Snapshot.id == snapshot.id)
        .options(selectinload(Snapshot.contents), selectinload(Snapshot.headers))
    )
    result = await db_session.execute(stmt)
    loaded = result.scalar_one()

    assert loaded.url == "https://timeout.example.com"
    assert loaded.http_status is None  # unknown — goto never returned
    assert loaded.condition_type == ConditionType.lifecycle
    assert loaded.condition == "load"
    assert loaded.condition_met is False
    assert loaded.error is not None  # Playwright timeout message
    assert loaded.lifecycle_events == []

    # No lifecycle events fired, so captures were skipped
    assert loaded.contents == []


@pytest.mark.asyncio
async def test_http_commit_captured_when_load_times_out(db_session, browser: Browser):
    """HTTP status/headers come from commit; load times out.

    The two-step navigation means we get the HTTP response even when the
    page never finishes loading. Captures still run because DOMContentLoaded
    fires (the DOM parses fine — only the `load` event stalls).
    """
    context = await browser.new_context()
    page = await context.new_page()

    # Block the image so `load` never fires, but serve the HTML fine.
    await page.route(
        "https://slow.example.com/slow-resource.png",
        lambda route: asyncio.sleep(60),  # never resolves
    )
    await page.route(
        "https://slow.example.com",
        lambda route: route.fulfill(
            body=BLOCK_LOAD_HTML,
            headers={"content-type": "text/html", "x-test": "yes"},
        ),
    )

    snapshot = await capture_page(
        page,
        "https://slow.example.com",
        db_session,
        condition="load",
        condition_timeout_ms=1,  # load will never fire; don't wait long
    )

    await context.close()

    stmt = (
        select(Snapshot)
        .where(Snapshot.id == snapshot.id)
        .options(selectinload(Snapshot.contents), selectinload(Snapshot.headers))
    )
    result = await db_session.execute(stmt)
    loaded = result.scalar_one()

    # HTTP response was captured from the commit step
    assert loaded.http_status == 200

    # load timed out
    assert loaded.condition_met is False
    assert loaded.error is not None

    # DOMContentLoaded fired, so captures were not skipped
    content_types = {c.content_type for c in loaded.contents}
    assert "multipart/related" in content_types
    assert "text/html" in content_types

    # Headers were captured
    header_names = {h.name for h in loaded.headers}
    assert "content-type" in header_names
    assert "x-test" in header_names

    # Lifecycle events include DOMContentLoaded but not load
    assert "DOMContentLoaded" in loaded.lifecycle_events
    assert "load" not in loaded.lifecycle_events
