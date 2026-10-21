"""Finalization failures are returned as failed snapshots."""

import pytest
from playwright.async_api import BrowserContext
from playwright.async_api import Error as PlaywrightError

import pravda.pravda as pravda_module
from pravda import Pravda
from pravda.capture import capture_page
from tests.helpers import fulfill_html


async def _record_example(browser, tmp_path, timeouts):
    http_archive_path = tmp_path / "record.zip"
    context = await browser.new_context(
        record_har_path=str(http_archive_path),
        record_har_content="attach",
    )
    page = await context.new_page()
    await page.route("https://example.com", fulfill_html)
    result = await capture_page(page, "https://example.com", timeouts)
    return context, result, http_archive_path


@pytest.mark.asyncio
async def test_context_close_failure_returns_failed_snapshot(
    pravda: Pravda, monkeypatch
):
    """A context.close failure finalizes nothing: a failed snapshot returns."""

    async def failing_close(self):
        raise OSError("context teardown lost the connection")

    monkeypatch.setattr(BrowserContext, "close", failing_close)

    async def drive(page, url):
        await page.route(url, fulfill_html)
        await page.goto(url, wait_until="load")

    snapshot = await pravda.capture("https://example.com", drive=drive)

    assert "browser context close failed" in snapshot.error
    assert "context teardown lost the connection" in snapshot.error
    assert snapshot.http_status is None
    assert snapshot.rendered_html is None
    assert snapshot.http_archive is None


@pytest.mark.asyncio
async def test_missing_http_archive_fails_finalization(
    browser, storage, tmp_path, timeouts
):
    """A close that leaves no HAR fails finalization; capture records it."""
    context, captured, path = await _record_example(browser, tmp_path, timeouts)
    await context.close()
    path.unlink()

    with pytest.raises(
        pravda_module._FinalizationError,
        match="closed without producing an HTTP archive",
    ):
        await pravda_module._store_artifacts(captured, path, storage)


@pytest.mark.asyncio
async def test_capture_har_storage_failure_propagates(pravda: Pravda, monkeypatch):
    """A storage failure during HAR processing propagates."""

    async def drive(page, url):
        await page.route(url, fulfill_html)
        await page.goto(url, wait_until="load")

        async def fail_screenshot(**kwargs):
            raise PlaywrightError("screenshot unavailable")

        page.screenshot = fail_screenshot

    original_pipe_file = pravda._storage.fs._pipe_file
    writes = 0

    async def fail_har_pipe_file(path, value, **kwargs):
        nonlocal writes
        writes += 1
        if writes > 1:
            raise OSError("storage backend down")
        await original_pipe_file(path, value, **kwargs)

    monkeypatch.setattr(pravda._storage.fs, "_pipe_file", fail_har_pipe_file)

    with pytest.raises(OSError, match="storage backend down"):
        await pravda.capture("https://example.com", drive=drive)
