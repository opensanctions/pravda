"""Atomic failure semantics for HAR finalization."""

import asyncio
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

import pravda.pravda as pravda_module
from pravda import Pravda
from pravda.capture import capture_page
from pravda.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"


def _fulfill_html(route):
    return route.fulfill(
        body=(FIXTURES / "example.html").read_text(),
        headers={"content-type": "text/html"},
    )


async def _record_example(browser, storage: Storage, tmp_path):
    http_archive_path = tmp_path / "record.zip"
    context = await browser.new_context(
        record_har_path=str(http_archive_path),
        record_har_content="attach",
    )
    page = await context.new_page()
    await page.route("https://example.com", _fulfill_html)
    result = await capture_page(page, "https://example.com", storage)
    return context, result, http_archive_path


@pytest.mark.asyncio
async def test_context_close_failure_discards_evidence(browser, storage, tmp_path):
    """An unfinalized recording context invalidates all captured evidence."""
    context, captured, path = await _record_example(browser, storage, tmp_path)

    async def failing_close():
        raise OSError("context teardown lost the connection")

    context.close = failing_close
    result, http_archive = await pravda_module._finalize_capture(
        context, captured, path, "https://example.com/", storage
    )

    assert result.error == (
        "browser context close failed: context teardown lost the connection"
    )
    assert result.http_status is None
    assert result.final_url is None
    assert result.plaintext is None
    assert result.rendered_html is None
    assert result.screenshot is None
    assert http_archive is None


@pytest.mark.asyncio
async def test_har_processing_timeout_propagates(
    browser, storage: Storage, tmp_path, monkeypatch
):
    """A HAR storage timeout fails the operation rather than persisting a fallback."""
    context, captured, path = await _record_example(browser, storage, tmp_path)
    monkeypatch.setattr(pravda_module, "HAR_PROCESSING_TIMEOUT_S", 0.01)

    async def slow_pipe_file(path, value, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(storage.fs, "_pipe_file", slow_pipe_file)

    with pytest.raises(asyncio.TimeoutError):
        await pravda_module._finalize_capture(
            context, captured, path, "https://example.com/", storage
        )


@pytest.mark.asyncio
async def test_snapshot_har_storage_failure_propagates_without_persisting(
    pravda: Pravda, monkeypatch
):
    """A storage failure during HAR processing propagates and persists nothing."""

    async def drive(page, url):
        await page.route(url, _fulfill_html)
        await page.goto(url, wait_until="load")

        async def fail_screenshot(**kwargs):
            raise PlaywrightError("screenshot unavailable")

        page.screenshot = fail_screenshot

    original_pipe_file = pravda._storage.fs._pipe_file
    writes = 0

    async def fail_har_pipe_file(path, value, **kwargs):
        nonlocal writes
        writes += 1
        if writes > 2:
            raise OSError("storage backend down")
        await original_pipe_file(path, value, **kwargs)

    monkeypatch.setattr(pravda._storage.fs, "_pipe_file", fail_har_pipe_file)

    with pytest.raises(OSError, match="storage backend down"):
        await pravda.snapshot("https://example.com", drive=drive)

    assert await pravda.snapshots("https://example.com") == []
