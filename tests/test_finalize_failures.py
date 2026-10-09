"""Atomic failure semantics for HAR finalization."""

import asyncio
from dataclasses import replace

import pytest
from playwright.async_api import Error as PlaywrightError

import pravda.pravda as pravda_module
from pravda import Pravda, Timeouts
from pravda.capture import capture_page
from pravda.storage import Storage
from tests.helpers import fulfill_html, slow_pipe_file


async def _record_example(browser, tmp_path):
    http_archive_path = tmp_path / "record.zip"
    context = await browser.new_context(
        record_har_path=str(http_archive_path),
        record_har_content="attach",
    )
    page = await context.new_page()
    await page.route("https://example.com", fulfill_html)
    result = await capture_page(page, "https://example.com")
    return context, result, http_archive_path


@pytest.mark.asyncio
async def test_context_close_failure_discards_evidence(browser, storage, tmp_path):
    """An unfinalized recording context invalidates all captured evidence."""
    context, captured, path = await _record_example(browser, tmp_path)

    async def failing_close():
        raise OSError("context teardown lost the connection")

    context.close = failing_close
    with pytest.raises(
        pravda_module._CaptureFailure,
        match="browser context close failed: context teardown lost the connection",
    ):
        await pravda_module._finalize_capture(
            context, captured, path, storage, Timeouts()
        )


@pytest.mark.asyncio
async def test_har_processing_timeout_propagates(
    browser, storage: Storage, tmp_path, monkeypatch
):
    """A HAR storage timeout fails the operation rather than persisting a fallback."""
    context, captured, path = await _record_example(browser, tmp_path)

    monkeypatch.setattr(storage.fs, "_pipe_file", slow_pipe_file)

    timeouts = replace(Timeouts(), har_processing_milliseconds=10)
    with pytest.raises(asyncio.TimeoutError):
        await pravda_module._finalize_capture(
            context, captured, path, storage, timeouts
        )


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
