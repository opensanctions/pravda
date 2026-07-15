"""Finalization-stage failure and timeout semantics."""

import asyncio
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

import pravda.pravda as pravda_module
from pravda import Pravda
from pravda.capture import CaptureResult, capture_page
from pravda.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"


def _fulfill_html(route):
    return route.fulfill(
        body=(FIXTURES / "example.html").read_text(),
        headers={"content-type": "text/html"},
    )


async def _capture_example(page, storage: Storage) -> CaptureResult:
    await page.route("https://example.com", _fulfill_html)
    return await capture_page(page, "https://example.com", storage)


@pytest.mark.asyncio
async def test_context_close_failure_keeps_evidence(
    browser, storage: Storage, tmp_path
):
    """A failed context.close keeps evidence and records a fatal error."""
    http_archive_path = tmp_path / "record.zip"
    context = await browser.new_context(
        record_har_path=str(http_archive_path),
        record_har_content="attach",
    )
    page = await context.new_page()
    captured = await _capture_example(page, storage)

    async def failing_close():
        raise OSError("context teardown lost the connection")

    context.close = failing_close

    result, http_archive = await pravda_module._finalize_capture(
        context, captured, http_archive_path, "https://example.com/", {}, storage
    )

    assert result.http_status == 200
    assert result.final_url == "https://example.com/"
    assert result.rendered_html == captured.rendered_html
    assert result.plaintext == captured.plaintext
    assert result.screenshot == captured.screenshot
    assert "context close failed" in result.error
    assert http_archive is None


@pytest.mark.asyncio
async def test_context_close_failure_composes_with_prior_error(
    browser, storage: Storage, tmp_path
):
    """A context.close failure is composed onto a prior capture error."""
    http_archive_path = tmp_path / "record.zip"
    context = await browser.new_context(
        record_har_path=str(http_archive_path),
        record_har_content="attach",
    )
    page = await context.new_page()
    captured = await _capture_example(page, storage)
    prior_error = "Timeout 30000ms exceeded waiting for 'load'"
    captured_with_error = CaptureResult(
        http_status=captured.http_status,
        error=prior_error,
        final_url=captured.final_url,
        plaintext=captured.plaintext,
        rendered_html=captured.rendered_html,
        screenshot=captured.screenshot,
        download=None,
    )

    async def failing_close():
        raise OSError("context teardown lost the connection")

    context.close = failing_close

    result, http_archive = await pravda_module._finalize_capture(
        context,
        captured_with_error,
        http_archive_path,
        "https://example.com/",
        {},
        storage,
    )

    assert prior_error in result.error
    assert "context close failed" in result.error
    assert result.rendered_html == captured.rendered_html
    assert http_archive is None


@pytest.mark.asyncio
async def test_har_processing_timeout_propagates(
    browser, storage: Storage, tmp_path, monkeypatch
):
    """A HAR processing timeout fails finalization."""
    http_archive_path = tmp_path / "record.zip"
    context = await browser.new_context(
        record_har_path=str(http_archive_path),
        record_har_content="attach",
    )
    page = await context.new_page()
    captured = await _capture_example(page, storage)

    # Artifacts are already stored; stall the backend and tighten the HAR budget.
    monkeypatch.setattr(pravda_module, "HAR_PROCESSING_TIMEOUT_S", 0.01)

    async def slow_pipe_file(path, value, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(storage.fs, "_pipe_file", slow_pipe_file)

    with pytest.raises(asyncio.TimeoutError):
        await pravda_module._finalize_capture(
            context, captured, http_archive_path, "https://example.com/", {}, storage
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
