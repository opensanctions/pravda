"""Finalization-stage failure and timeout semantics.

Operational failures and timeouts while closing the recording context or
processing the HAR must not erase the page evidence already captured or let
the capture escape without persistence. The context-close failure and HAR
timeout paths are exercised directly against ``_finalize_capture`` — the
boundary where they can be induced deterministically — while the end-to-end
persistence behavior is verified through ``Pravda.snapshot``.
"""

import asyncio
from pathlib import Path

import pytest

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
    """A context.close that fails operationally keeps the page evidence,
    records a fatal error, and discards the HAR."""
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

    # Page evidence is retained exactly; only a fatal error is added and the
    # (unusable) HAR is dropped.
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
    """A context.close failure is composed onto an error the capture already
    recorded (e.g. a load timeout) instead of overwriting it."""
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

    # Both the prior capture error and the finalization error survive.
    assert prior_error in result.error
    assert "context close failed" in result.error
    assert result.rendered_html == captured.rendered_html
    assert http_archive is None


@pytest.mark.asyncio
async def test_har_processing_timeout_keeps_evidence(
    browser, storage: Storage, tmp_path, monkeypatch
):
    """A HAR processing timeout keeps the page evidence, marks the snapshot
    fatal, and drops the HAR."""
    http_archive_path = tmp_path / "record.zip"
    context = await browser.new_context(
        record_har_path=str(http_archive_path),
        record_har_content="attach",
    )
    page = await context.new_page()
    captured = await _capture_example(page, storage)

    # capture_page already stored its artifacts; stall the storage backend and
    # tighten the HAR budget so unpacking the archive exceeds it.
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


@pytest.mark.asyncio
async def test_snapshot_har_storage_failure_persists_attempt(
    pravda: Pravda, monkeypatch
):
    """An operational storage failure during HAR processing does not let the
    capture escape: the snapshot is persisted with the page metadata, a HAR
    error composed onto any capture error, and no HAR. The same failing backend
    also drops the page artifacts (their writes are isolated); the point is
    that the attempt is still committed."""

    async def drive(page, url):
        await page.route(url, _fulfill_html)
        await page.goto(url, wait_until="load")

    async def failing_pipe_file(path, value, **kwargs):
        raise OSError("storage backend down")

    monkeypatch.setattr(pravda._storage.fs, "_pipe_file", failing_pipe_file)

    snapshot = await pravda.snapshot("https://example.com", drive=drive)

    assert snapshot.error is not None
    assert "HAR processing failed" in snapshot.error
    assert snapshot.http_archive is None
    # Page metadata persisted despite the storage failure.
    assert snapshot.http_status == 200
    assert snapshot.final_url == "https://example.com/"
    # The failing backend dropped the page artifacts too (isolated writes).
    assert snapshot.rendered_html is None
    assert snapshot.plaintext is None
    assert snapshot.screenshot is None

    history = await pravda.snapshots("https://example.com")
    assert any(item.id == snapshot.id for item in history)
