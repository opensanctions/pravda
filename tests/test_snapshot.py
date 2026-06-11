from pathlib import Path

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pravda.capture import capture_page
from pravda.db import ConditionType, Snapshot

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
async def test_capture_page_timeout_no_lifecycle_skips_captures(db_session):
    """A timeout before any lifecycle event fires skips captures entirely."""

    async with async_playwright() as p:
        browser = await p.chromium.connect("ws://localhost:3000")
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


# --- _lifecycle_reached unit tests ---


def test_lifecycle_reached_domcontentloaded_suffices():
    from pravda.capture import _lifecycle_reached

    events = ["init", "commit", "DOMContentLoaded"]
    assert _lifecycle_reached(events, "DOMContentLoaded") is True


def test_lifecycle_reached_later_event_implies_earlier():
    from pravda.capture import _lifecycle_reached

    events = ["init", "commit", "DOMContentLoaded", "firstPaint"]
    assert _lifecycle_reached(events, "DOMContentLoaded") is True
    assert _lifecycle_reached(events, "firstPaint") is True
    assert _lifecycle_reached(events, "load") is False


def test_lifecycle_reached_empty_list():
    from pravda.capture import _lifecycle_reached

    assert _lifecycle_reached([], "DOMContentLoaded") is False


def test_lifecycle_reached_only_init_and_commit():
    from pravda.capture import _lifecycle_reached

    events = ["init", "commit"]
    assert _lifecycle_reached(events, "DOMContentLoaded") is False
    assert _lifecycle_reached(events, "commit") is True
