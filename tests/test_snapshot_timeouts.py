"""Fatal-evidence semantics for context.close and HAR-processing timeouts.

A close or HAR timeout must not discard the page evidence already captured.
These tests drive the real Playwright/storage boundary used by the capture
library: a real browser context captures evidence through a routed fixture,
then the finalization helper (``_finalize_capture``) is exercised with a
stalled boundary so the timeout path fires deterministically.
"""

import asyncio
import json
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

import pravda.pravda as pravda_module
from pravda import PravdaConfig
from pravda.capture import capture_page
from pravda.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_context_close_timeout_keeps_evidence(
    pravda_config: PravdaConfig, storage: Storage, monkeypatch, tmp_path
):
    """A context.close that exceeds its budget keeps the page evidence,
    marks the snapshot fatal, drops the incomplete HAR, and the forced
    browser cleanup still succeeds."""
    fixture_html = (FIXTURES / "example.html").read_text()
    monkeypatch.setattr(pravda_module, "CONTEXT_CLOSE_TIMEOUT_S", 0.01)

    # The test owns its browser connection so it can exercise the forced
    # browser.close() cleanup after the wedged context.close().
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect(
        pravda_config.browser_ws_url,
        headers={
            "x-playwright-launch-options": json.dumps(
                {"channel": pravda_module.BROWSER_CHANNEL, "headless": False}
            )
        },
    )
    try:
        http_archive_path = tmp_path / "record.zip"
        context = await browser.new_context(
            record_har_path=str(http_archive_path),
            record_har_content="attach",
        )
        page = await context.new_page()

        async def slow_context_close():
            await asyncio.sleep(1)

        context.close = slow_context_close
        await page.route(
            "https://example.com",
            lambda route: route.fulfill(
                body=fixture_html, headers={"content-type": "text/html"}
            ),
        )
        captured = await capture_page(page, "https://example.com", storage)

        result, http_archive = await pravda_module._finalize_capture(
            context, captured, http_archive_path, "https://example.com/", {}, storage
        )

        # Page evidence is retained exactly; only a fatal error is added and
        # the (incomplete) HAR is dropped.
        assert result.http_status == 200
        assert result.final_url == "https://example.com/"
        assert result.rendered_html == captured.rendered_html
        assert result.plaintext == captured.plaintext
        assert result.screenshot == captured.screenshot
        assert result.error is not None
        assert "close" in result.error
        assert http_archive is None

        # The snapshot state is already final; forced browser cleanup remains
        # safe after the context-close timeout.
        await pravda_module._close_browser(browser)
        browser = None
    finally:
        if browser is not None:
            await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_har_processing_timeout_keeps_evidence(
    browser, storage: Storage, monkeypatch, tmp_path
):
    """HAR processing that exceeds its budget keeps the page evidence,
    marks the snapshot fatal, and drops the HAR."""
    fixture_html = (FIXTURES / "example.html").read_text()

    http_archive_path = tmp_path / "record.zip"
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
    captured = await capture_page(page, "https://example.com", storage)

    # capture_page already stored its artifacts; now stall the storage backend
    # and tighten the HAR budget so unpacking the archive exceeds it. Only the
    # HAR body writes are affected.
    monkeypatch.setattr(pravda_module, "HAR_PROCESSING_TIMEOUT_S", 0.01)

    async def slow_pipe_file(path, value, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(storage.fs, "_pipe_file", slow_pipe_file)

    result, http_archive = await pravda_module._finalize_capture(
        context, captured, http_archive_path, "https://example.com/", {}, storage
    )

    assert result.http_status == 200
    assert result.final_url == "https://example.com/"
    assert result.rendered_html == captured.rendered_html
    assert result.plaintext == captured.plaintext
    assert result.screenshot == captured.screenshot
    assert result.error is not None
    assert "HAR" in result.error
    assert http_archive is None
