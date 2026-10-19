"""Finalization failures raise and persist nothing."""

import pytest
from playwright.async_api import Error as PlaywrightError

import pravda.pravda as pravda_module
from pravda import InfrastructureError, Liveness, Pravda, Timeouts
from pravda.capture import capture_page
from tests.helpers import fulfill_html


async def _record_example(browser, tmp_path, timeouts: Timeouts):
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
async def test_context_close_failure_raises(browser, storage, tmp_path, timeouts):
    """A context.close failure is infrastructure: it raises, persisting nothing."""
    context, captured, path = await _record_example(browser, tmp_path, timeouts)

    async def failing_close():
        raise OSError("context teardown lost the connection")

    context.close = failing_close
    with pytest.raises(
        InfrastructureError,
        match="browser context close failed: context teardown lost the connection",
    ):
        await pravda_module._close_context(context, Liveness())


@pytest.mark.asyncio
async def test_missing_http_archive_raises(browser, storage, tmp_path, timeouts):
    """A close that leaves no HAR is infrastructure: it raises, persisting nothing."""
    context, captured, path = await _record_example(browser, tmp_path, timeouts)
    await context.close()
    path.unlink()

    with pytest.raises(
        InfrastructureError, match="closed without producing an HTTP archive"
    ):
        await pravda_module._store_artifacts(captured, path, storage)


@pytest.mark.asyncio
async def test_snapshot_har_storage_failure_propagates_without_persisting(
    pravda: Pravda, monkeypatch
):
    """A storage failure during HAR processing propagates and persists nothing."""

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
        await pravda.snapshot("https://example.com", drive=drive)

    assert await pravda.snapshots("https://example.com") == []
