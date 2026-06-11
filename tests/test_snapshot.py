import asyncio
from pathlib import Path

import pytest
from playwright.async_api import Browser
from playwright.async_api import TimeoutError as PlaywrightTimeout
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pravda.api import SnapshotCreate, _build_snapshot
from pravda.capture import CapturedContent, CaptureResult, capture_page
from pravda.db import Snapshot

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_capture_page_returns_evidence(browser: Browser):
    """Capture a page using a routed fixture and inspect the evidence."""
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

    result = await capture_page(page, "https://example.com")

    await context.close()

    assert result.http_status == 200
    assert result.condition_met is True
    assert result.error is None

    content_types = {c.content_type for c in result.contents}
    assert content_types == {
        "multipart/related",
        "image/png",
        "text/html",
        "text/plain",
    }

    # The MHTML content hash should correspond to the fixture HTML
    mhtml = next(c for c in result.contents if c.content_type == "multipart/related")
    assert len(mhtml.hash) == 64  # sha256 hex

    screenshot = next(c for c in result.contents if c.content_type == "image/png")
    assert len(screenshot.hash) == 64

    assert "content-type" in result.headers


@pytest.mark.asyncio
async def test_capture_page_timeout_no_lifecycle_skips_captures(browser: Browser):
    """A timeout before any lifecycle event fires skips captures entirely."""

    context = await browser.new_context()
    page = await context.new_page()

    # Mock goto to raise timeout immediately — no real waiting,
    # no lifecycle events fire.
    async def fake_goto(*args, **kwargs):
        raise PlaywrightTimeout("Navigation timeout")

    page.goto = fake_goto

    result = await capture_page(page, "https://timeout.example.com", condition="load")

    await context.close()

    assert result.http_status is None  # unknown — goto never returned
    assert result.condition_met is False
    assert result.error is not None  # Playwright timeout message
    assert result.lifecycle_events == []

    # No lifecycle events fired, so captures were skipped
    assert result.contents == []


@pytest.mark.asyncio
async def test_http_commit_captured_when_load_times_out(browser: Browser):
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
            body=(FIXTURES / "blocking.html").read_text(),
            headers={"content-type": "text/html", "x-test": "yes"},
        ),
    )

    result = await capture_page(
        page,
        "https://slow.example.com",
        condition="load",
        condition_timeout_ms=1,  # load will never fire; don't wait long
    )

    await context.close()

    # HTTP response was captured from the commit step
    assert result.http_status == 200

    # load timed out
    assert result.condition_met is False
    assert result.error is not None

    # DOMContentLoaded fired, so captures were not skipped
    content_types = {c.content_type for c in result.contents}
    assert "multipart/related" in content_types
    assert "text/html" in content_types

    # Headers were captured
    assert "content-type" in result.headers
    assert "x-test" in result.headers

    # Lifecycle events include DOMContentLoaded but not load
    assert "DOMContentLoaded" in result.lifecycle_events
    assert "load" not in result.lifecycle_events


@pytest.mark.asyncio
async def test_captured_evidence_persists(db_session):
    """Evidence captured for a page maps onto a Snapshot and round-trips."""
    body = SnapshotCreate(url="https://example.com")
    result = CaptureResult(
        http_status=200,
        error=None,
        condition_met=True,
        lifecycle_events=["init", "commit", "DOMContentLoaded", "load"],
        headers={"content-type": "text/html"},
        contents=[CapturedContent(content_type="text/html", hash="a" * 64)],
    )

    snapshot = _build_snapshot(body, result)
    db_session.add(snapshot)
    await db_session.flush()

    loaded = (
        await db_session.execute(
            select(Snapshot)
            .where(Snapshot.id == snapshot.id)
            .options(selectinload(Snapshot.contents), selectinload(Snapshot.headers))
        )
    ).scalar_one()

    assert loaded.url == "https://example.com/"
    assert loaded.http_status == 200
    assert loaded.condition_met is True
    assert loaded.lifecycle_events == ["init", "commit", "DOMContentLoaded", "load"]
    assert [(c.content_type, c.hash) for c in loaded.contents] == [
        ("text/html", "a" * 64)
    ]
    assert [(h.name, h.value) for h in loaded.headers] == [
        ("content-type", "text/html")
    ]
