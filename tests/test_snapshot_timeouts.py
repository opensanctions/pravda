"""Fatal-evidence semantics for context.close and HAR-processing timeouts.

A close or HAR timeout must not discard the page evidence already captured.
These tests drive the real Playwright/storage boundary used by the API layer:
a real browser context captures evidence through a routed fixture, then the
finalization helper (``_finalize_capture``) is exercised with a tightened
budget (or a stalled storage backend) so the timeout path fires. The forced
browser cleanup is bounded too — verified against its own deadline.
"""

import asyncio
import json
import tempfile
import time
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

import pravda.api as api_module
import pravda.storage as storage
from pravda.capture import capture_page

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_context_close_timeout_keeps_evidence_and_bounds_cleanup(monkeypatch):
    """A context.close that exceeds its budget keeps the page evidence,
    marks the snapshot fatal, drops the incomplete HAR, and the forced
    browser cleanup is bounded."""
    fixture_html = (FIXTURES / "example.html").read_text()
    # context.close() normally takes ~20ms; a 1ms budget trips the close-timeout
    # path deterministically without patching Playwright.
    monkeypatch.setattr(api_module, "CONTEXT_CLOSE_TIMEOUT_S", 0.001)

    # The test owns its browser connection so it can exercise the forced
    # browser.close() cleanup after the wedged context.close().
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect(
        api_module.BROWSER_WS_URL,
        headers={
            "x-playwright-launch-options": json.dumps(
                {"channel": api_module.BROWSER_CHANNEL, "headless": False}
            )
        },
    )
    try:
        http_archive_path = Path(tempfile.mkdtemp()) / "record.zip"
        context = await browser.new_context(
            record_har_path=str(http_archive_path),
            record_har_content="attach",
        )
        page = await context.new_page()
        await page.route(
            "https://example.com",
            lambda route: route.fulfill(
                body=fixture_html, headers={"content-type": "text/html"}
            ),
        )
        captured = await capture_page(page, "https://example.com")

        result, http_archive = await api_module._finalize_capture(
            context, captured, http_archive_path, "https://example.com/", {}
        )

        # Page evidence is retained exactly; only a fatal error is added and
        # the (incomplete) HAR is dropped.
        assert result.http_status == 200
        assert result.condition_met is True
        assert result.final_url == "https://example.com/"
        assert result.rendered_html == captured.rendered_html
        assert result.plaintext == captured.plaintext
        assert result.screenshot == captured.screenshot
        assert result.error is not None
        assert "close" in result.error
        assert http_archive is None

        # Forced browser cleanup after the wedged close completes promptly
        # under its bound — the snapshot state above is already final.
        start = time.monotonic()
        await api_module._close_browser(browser)
        assert time.monotonic() - start < 2
        browser = None
    finally:
        if browser is not None:
            await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_har_processing_timeout_keeps_evidence(browser, monkeypatch):
    """HAR processing that exceeds its budget keeps the page evidence,
    marks the snapshot fatal, and drops the HAR."""
    fixture_html = (FIXTURES / "example.html").read_text()

    http_archive_path = Path(tempfile.mkdtemp()) / "record.zip"
    context = await browser.new_context(
        record_har_path=str(http_archive_path),
        record_har_content="attach",
    )
    page = await context.new_page()
    await page.route(
        "https://example.com",
        lambda route: route.fulfill(
            body=fixture_html, headers={"content-type": "text/html"}
        ),
    )
    captured = await capture_page(page, "https://example.com")

    # capture_page already stored its artifacts; now stall the storage backend
    # and tighten the HAR budget so unpacking the archive exceeds it. Only the
    # HAR body writes are affected.
    monkeypatch.setattr(api_module, "HAR_PROCESSING_TIMEOUT_S", 0.01)

    async def slow_pipe_file(path, value, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(storage.fs, "_pipe_file", slow_pipe_file)

    result, http_archive = await api_module._finalize_capture(
        context, captured, http_archive_path, "https://example.com/", {}
    )

    assert result.http_status == 200
    assert result.condition_met is True
    assert result.final_url == "https://example.com/"
    assert result.rendered_html == captured.rendered_html
    assert result.plaintext == captured.plaintext
    assert result.screenshot == captured.screenshot
    assert result.error is not None
    assert "HAR" in result.error
    assert http_archive is None
