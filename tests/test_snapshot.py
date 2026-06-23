import asyncio
from pathlib import Path

import pytest
from playwright.async_api import Browser
from playwright.async_api import TimeoutError as PlaywrightTimeout
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pravda.api import SnapshotCreate, _build_snapshot
from pravda.capture import CaptureResult, capture_page
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

    # All four artifacts captured, each a sha256 hex hash
    assert len(result.plaintext_hash) == 64
    assert len(result.rendered_html_hash) == 64
    assert len(result.screenshot_hash) == 64
    assert len(result.blob_hash) == 64
    assert result.blob_content_type == "multipart/related"

    assert "content-type" in result.headers


@pytest.mark.asyncio
async def test_capture_page_goto_timeout_skips_captures(browser: Browser):
    """A navigation that never commits skips captures entirely."""

    context = await browser.new_context()
    page = await context.new_page()

    # Mock goto to raise timeout immediately — the page never commits.
    async def fake_goto(*args, **kwargs):
        raise PlaywrightTimeout("Navigation timeout")

    page.goto = fake_goto

    result = await capture_page(page, "https://timeout.example.com", condition="load")

    await context.close()

    assert result.http_status is None  # unknown — goto never returned
    assert result.condition_met is False
    assert result.error is not None  # Playwright timeout message

    # Navigation never committed, so there is nothing to capture
    assert result.plaintext_hash is None
    assert result.rendered_html_hash is None
    assert result.screenshot_hash is None
    assert result.blob_hash is None
    assert result.blob_content_type is None


@pytest.mark.asyncio
async def test_http_commit_captured_when_load_times_out(browser: Browser):
    """HTTP status/headers come from commit; load times out.

    The two-step navigation means we get the HTTP response even when the
    page never finishes loading. Captures still run because DOMContentLoaded
    fires (the DOM parses fine — only the `load` event stalls on the image).
    """
    context = await browser.new_context()
    page = await context.new_page()

    # Hang the image so `load` never fires, but serve the HTML fine.
    await page.route(
        "https://slow.example.com/slow-resource.png",
        lambda route: asyncio.Event().wait(),  # never resolves
    )
    await page.route(
        "https://slow.example.com",
        lambda route: route.fulfill(
            body=(FIXTURES / "blocking.html").read_text(),
            headers={"content-type": "text/html", "x-test": "yes"},
        ),
    )

    # `load` waits on the blocked image and times out; DOMContentLoaded fires
    # almost immediately. A short timeout just keeps the test fast.
    result = await capture_page(
        page,
        "https://slow.example.com",
        condition="load",
        condition_timeout_ms=2000,
    )

    await context.close()

    # HTTP response was captured from the commit step
    assert result.http_status == 200

    # load timed out
    assert result.condition_met is False
    assert result.error is not None

    # Navigation committed, so every capture ran. The screenshot went
    # through despite load timing out: pending requests are stopped first so
    # the page settles into a capturable state.
    assert result.plaintext_hash is not None
    assert result.rendered_html_hash is not None
    assert result.screenshot_hash is not None
    assert result.blob_hash is not None
    assert result.blob_content_type == "multipart/related"

    # Headers were captured
    assert "content-type" in result.headers
    assert "x-test" in result.headers


@pytest.mark.asyncio
async def test_captured_evidence_persists(db_session):
    """Evidence captured for a page maps onto a Snapshot and round-trips."""
    body = SnapshotCreate(url="https://example.com")
    result = CaptureResult(
        http_status=200,
        error=None,
        condition_met=True,
        headers={"content-type": "text/html"},
        plaintext_hash=None,
        rendered_html_hash="a" * 64,
        screenshot_hash=None,
        blob_hash=None,
        blob_content_type=None,
    )

    snapshot = _build_snapshot(body, result)
    db_session.add(snapshot)
    await db_session.flush()

    loaded = (
        await db_session.execute(
            select(Snapshot)
            .where(Snapshot.id == snapshot.id)
            .options(selectinload(Snapshot.headers))
        )
    ).scalar_one()

    assert loaded.url == "https://example.com/"
    assert loaded.http_status == 200
    assert loaded.condition_met is True
    assert loaded.rendered_html == "a" * 64
    assert loaded.plaintext is None
    assert loaded.screenshot is None
    assert loaded.blob is None
    assert loaded.blob_content_type is None
    assert [(h.name, h.value) for h in loaded.headers] == [
        ("content-type", "text/html")
    ]
