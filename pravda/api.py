import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query
from playwright.async_api import Browser, BrowserContext, async_playwright
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, Field, HttpUrl, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pravda.capture import CaptureResult, capture_page
from pravda.db import ConditionType, Snapshot, get_session
from pravda.http_archive import capture_http_archive
from pravda.storage import content_prefix

BROWSER_CHANNEL = "chrome"
BROWSER_WS_URL = os.environ["BROWSER_WS_URL"]

# Wall-clock budget for the whole snapshot pipeline (connect → setup →
# capture → context.close → HAR). capture_page bounds the page interactions
# internally, and the stages below add tighter inner bounds for the calls
# that reach the remote browser server over a pipe. This outer budget is the
# breaker of last resort: it catches a stage that eludes its inner bound —
# notably page capture — converting a silent hang into a bounded failure.
SNAPSHOT_TIMEOUT_S = 75

# Timeout for the handshake with the remote browser server (connect + launch).
# Playwright defaults connect's timeout to 0 (no timeout); bounding it makes a
# dead server fail fast as a PlaywrightError instead of waiting on the
# wall-clock budget.
CONNECT_TIMEOUT_MS = 10_000

# Combined budget for browser.new_context() + context.new_page(). Both reach
# the remote server over a pipe; bounding them catches a wedge before a page
# even exists.
SETUP_TIMEOUT_S = 10

# Budget for unpacking the HAR zip and storing every recorded body, as one
# stage. Individual normal-artifact writes (rendered HTML, plaintext,
# screenshot, downloaded file) are bounded separately inside capture_page.
# On timeout the page evidence is kept and the HAR is discarded.
HAR_PROCESSING_TIMEOUT_S = 20

# Budget for BrowserContext.close(), which flushes the HAR zip. close() has no
# Playwright timeout argument; bounding it preserves the page evidence already
# captured and discards an incomplete HAR instead of hanging.
CONTEXT_CLOSE_TIMEOUT_S = 10

# Budget for the forced browser.close() cleanup that runs after capture is
# finalized. browser.close() has no Playwright timeout argument; bounding it
# guarantees cleanup cannot wedge the request. A timeout here is logged as an
# operational warning only and never alters the finalized snapshot.
BROWSER_CLOSE_TIMEOUT_S = 5

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        filename="pravda.log",
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    yield


app = FastAPI(
    title="Pravda",
    description="Evidence layer for web pages",
    lifespan=lifespan,
)


# --- Request / response models ---


class SnapshotCreate(BaseModel):
    """Request body for creating a new snapshot."""

    url: HttpUrl
    condition_type: Annotated[
        ConditionType,
        Field(description="How to determine when the page is ready."),
    ] = ConditionType.lifecycle
    condition: Annotated[
        str,
        Field(
            description=(
                "Depends on condition_type: for 'lifecycle' a load state, "
                "one of 'load', 'domcontentloaded', 'networkidle', 'commit'. "
                "For 'selector', a CSS selector to wait for."
            )
        ),
    ] = "load"

    @model_validator(mode="after")
    def _validate_condition(self) -> "SnapshotCreate":
        if self.condition_type is ConditionType.lifecycle:
            valid = {"load", "domcontentloaded", "networkidle", "commit"}
            if self.condition not in valid:
                raise ValueError(
                    f"condition must be one of {valid} "
                    f"when condition_type is 'lifecycle', "
                    f"got '{self.condition}'"
                )
        return self


class SnapshotOut(BaseModel):
    """A captured snapshot of a web page.

    `prefix` is the full path to the directory under the shared storage
    backend where this snapshot's artifacts live (base path + normalized
    hostname of `final_url`). Each location below is a bare content-addressed filename
    (``<sha1>.<extension>``); downstream services resolve each as
    ``<prefix>/<filename>`` directly against their access to the same
    backend — there is no blob download endpoint. The HAR's
    ``content._file`` entries name bodies the same way, so they resolve
    identically. `prefix` is null when navigation never committed and no
    artifacts were stored.

    `plaintext`, `rendered_html`, and `screenshot` are each null when that
    artifact was not captured (e.g. the page never committed, or the capture
    timed out). The file extension carries the artifact's type (txt, html,
    png). `http_archive` is the recorded HAR manifest, served inline as JSON;
    each entry's `response.content._file` names a body resolved as
    `<prefix>/<filename>`. `http_archive` is null when navigation never
    committed.
    """

    id: uuid.UUID
    url: HttpUrl = Field(description="The URL that was requested")
    final_url: HttpUrl | None = Field(
        default=None,
        description="The URL the page ended up at after redirects, or null",
    )
    captured_at: datetime = Field(description="When the snapshot was taken (UTC)")
    http_status: int | None = Field(
        default=None,
        description="HTTP response status code, if a response was received",
    )
    error: str | None = Field(
        default=None,
        description="Error message if the capture failed",
    )
    condition_type: ConditionType = Field(description="Condition type that was used")
    condition: str = Field(description="Condition value that was used")
    condition_met: bool = Field(
        description="Whether the page condition was satisfied before capture",
    )
    prefix: str | None = Field(
        default=None,
        description=(
            "Full path (storage backend base + normalized hostname of "
            "final_url) under which this snapshot's artifacts live; resolve "
            "each filename below as ``<prefix>/<filename>``. Null when "
            "navigation never committed and no artifacts were stored."
        ),
    )
    plaintext: str | None = Field(
        default=None,
        description="Content-addressed filename of the page text (``.txt``), or null",
    )
    rendered_html: str | None = Field(
        default=None,
        description=(
            "Content-addressed filename of the rendered HTML (``.html``), or null"
        ),
    )
    screenshot: str | None = Field(
        default=None,
        description=(
            "Content-addressed filename of the full-page screenshot (``.png``), or null"
        ),
    )
    http_archive: dict | None = Field(
        default=None,
        description=(
            "The recorded HAR manifest (inline JSON), or null. Each entry's "
            "response.content._file names a content-addressed body resolved as "
            "<prefix>/<filename>."
        ),
    )


class SnapshotsOut(BaseModel):
    items: list[SnapshotOut]
    total: int = Field(description="Total number of snapshots for this URL")


# --- Endpoints ---


PAGE_SIZE = 10


@app.get("/snapshots", response_model=SnapshotsOut)
async def list_snapshots(
    url: str = Query(..., description="Exact URL to look up snapshots for"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    session: AsyncSession = Depends(get_session),
) -> SnapshotsOut:
    total_stmt = select(func.count()).select_from(Snapshot).where(Snapshot.url == url)
    total = (await session.execute(total_stmt)).scalar_one()

    rows_stmt = (
        select(Snapshot)
        .where(Snapshot.url == url)
        .order_by(Snapshot.captured_at.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    rows = (await session.execute(rows_stmt)).scalars().all()

    return SnapshotsOut(
        items=[_snapshot_out(row) for row in rows],
        total=total,
    )


def _snapshot_out(snapshot: Snapshot) -> SnapshotOut:
    return SnapshotOut(
        id=snapshot.id,
        url=snapshot.url,
        final_url=snapshot.final_url,
        captured_at=snapshot.captured_at,
        http_status=snapshot.http_status,
        error=snapshot.error,
        condition_type=snapshot.condition_type,
        condition=snapshot.condition,
        condition_met=snapshot.condition_met,
        prefix=content_prefix(snapshot.final_url) if snapshot.final_url else None,
        plaintext=snapshot.plaintext,
        rendered_html=snapshot.rendered_html,
        screenshot=snapshot.screenshot,
        http_archive=snapshot.http_archive,
    )


@app.post("/snapshots", response_model=SnapshotOut)
async def create_snapshot(
    body: SnapshotCreate,
    session: AsyncSession = Depends(get_session),
) -> SnapshotOut:
    logger.info(
        "Capturing %s (condition=%s:%s)",
        body.url,
        body.condition_type.value,
        body.condition,
    )
    http_archive_dir = Path(tempfile.mkdtemp())
    stages: dict[str, float] = {}
    timeout_error: str | None = None
    # Defaults for a capture that produced no evidence; the pipeline below
    # overwrites these on success or partial success.
    result = CaptureResult(
        http_status=None,
        error=None,
        condition_met=False,
        final_url=None,
        plaintext=None,
        rendered_html=None,
        screenshot=None,
        download=None,
    )
    http_archive: dict | None = None
    try:
        async with async_playwright() as playwright:
            # async_playwright() is the *outer* context manager so its teardown
            # is guaranteed to run once the pipeline below resolves (success, a
            # handled failure, or the outer breaker). Its teardown kills the
            # local driver subprocess, which is expected to be fast — but being
            # outside the deadline does not by itself prove it cannot hang, so
            # we also explicitly close the connected browser under a bounded
            # timeout in the finally below rather than relying on teardown
            # alone.
            browser: Browser | None = None
            try:
                # Whole-pipeline wall-clock breaker. Inner stages carry their
                # own tighter bounds (connect, setup, context.close, HAR);
                # capture_page bounds the page interactions internally. This
                # outer budget is the breaker of last resort for a stage that
                # eludes its inner bound (notably page capture), converting a
                # silent hang into a bounded failure.
                async with asyncio.timeout(SNAPSHOT_TIMEOUT_S):
                    stage_start = time.monotonic()
                    browser = await playwright.chromium.connect(
                        BROWSER_WS_URL,
                        # connect's timeout defaults to 0 (no timeout). Bound
                        # the handshake so a dead server fails fast as a
                        # PlaywrightError instead of waiting for the wall-clock
                        # budget above.
                        timeout=CONNECT_TIMEOUT_MS,
                        headers={
                            "x-playwright-launch-options": json.dumps(
                                {"channel": BROWSER_CHANNEL, "headless": False}
                            ),
                        },
                    )
                    stages["connect"] = time.monotonic() - stage_start

                    # Record a HAR of all network activity, with response
                    # bodies stored as separate entries inside a zip archive
                    # (record_har_content="attach" + a .zip path). The file is
                    # flushed when the context closes.
                    http_archive_path = http_archive_dir / "record.zip"
                    stage_start = time.monotonic()
                    try:
                        async with asyncio.timeout(SETUP_TIMEOUT_S):
                            context = await browser.new_context(
                                record_har_path=str(http_archive_path),
                                record_har_content="attach",
                            )
                            page = await context.new_page()
                    except asyncio.TimeoutError:
                        timeout_error = (
                            f"context/page setup exceeded {SETUP_TIMEOUT_S}s budget"
                        )
                        raise
                    stages["setup"] = time.monotonic() - stage_start

                    stage_start = time.monotonic()
                    result = await capture_page(
                        page,
                        str(body.url),
                        condition_type=body.condition_type,
                        condition=body.condition,
                    )
                    stages["capture"] = time.monotonic() - stage_start

                    # Close the context (flushing the HAR) and unpack the
                    # archive. Both carry fatal-evidence semantics on timeout:
                    # the page evidence already captured is kept, a precise
                    # error is recorded, and the (potentially incomplete) HAR
                    # is discarded.
                    result, http_archive = await _finalize_capture(
                        context,
                        result,
                        http_archive_path,
                        str(body.url),
                        stages,
                    )
            finally:
                # Disconnect the browser on every path once one was connected.
                # browser.close() clears contexts and disconnects from the
                # remote server; it is bounded so cleanup cannot wedge the
                # request, and a cleanup timeout/failure is an operational
                # warning only that never alters the finalized snapshot.
                if browser is not None:
                    await _close_browser(browser)
    except PlaywrightError as error:
        # Couldn't even reach the browser — record an empty, failed result.
        logger.error("Browser error for %s: %s", body.url, error.message)
        result = CaptureResult(
            http_status=None,
            error=error.message,
            condition_met=False,
            final_url=None,
            plaintext=None,
            rendered_html=None,
            screenshot=None,
            download=None,
        )
        http_archive = None
    except asyncio.TimeoutError:
        # The outer breaker (or the setup budget) tripped. timeout_error names
        # the setup stage when that fired; otherwise a stage that eluded its
        # inner bound hit the wall-clock budget. No partial evidence is
        # preserved here — the dedicated context.close/HAR handlers in
        # _finalize_capture own the partial-evidence policy for those stages.
        message = timeout_error or (
            f"snapshot exceeded {SNAPSHOT_TIMEOUT_S}s wall-clock budget"
        )
        logger.error("%s for %s", message, body.url)
        result = CaptureResult(
            http_status=None,
            error=message,
            condition_met=False,
            final_url=None,
            plaintext=None,
            rendered_html=None,
            screenshot=None,
            download=None,
        )
        http_archive = None
    finally:
        shutil.rmtree(http_archive_dir, ignore_errors=True)

    snapshot = _build_snapshot(body, result, http_archive)
    session.add(snapshot)
    stage_start = time.monotonic()
    await session.commit()
    stages["commit"] = time.monotonic() - stage_start
    logger.info(
        "Captured %s id=%s: status=%s condition_met=%s http_archive=%s error=%s "
        "timings=%s",
        snapshot.url,
        snapshot.id,
        snapshot.http_status,
        snapshot.condition_met,
        snapshot.http_archive is not None,
        snapshot.error,
        " ".join(f"{name}={duration:.2f}s" for name, duration in stages.items()),
    )
    return _snapshot_out(snapshot)


async def _finalize_capture(
    context: BrowserContext,
    result: CaptureResult,
    http_archive_path: Path,
    url: str,
    stages: dict[str, float],
) -> tuple[CaptureResult, dict | None]:
    """Close the context (flushing the HAR) and unpack the archive.

    ``context.close()`` has no Playwright timeout, so bound it. If it wedges,
    the HAR it would flush is incomplete and must be discarded — but the page
    evidence ``capture_page`` already returned is kept, with a fatal error
    recorded so the snapshot is not mistaken for a success. HAR processing
    follows the same policy on its own timeout.

    *url* is the page URL (for logging). Returns the (possibly error-amended)
    result and the HAR manifest, or ``None`` when navigation did not commit or
    the archive was discarded. *stages* records the ``close`` and ``har``
    durations for the completion log.
    """
    stage_start = time.monotonic()
    try:
        async with asyncio.timeout(CONTEXT_CLOSE_TIMEOUT_S):
            await context.close()
    except asyncio.TimeoutError:
        stages["close"] = time.monotonic() - stage_start
        logger.error(
            "context.close exceeded %ss budget for %s; discarding HAR",
            CONTEXT_CLOSE_TIMEOUT_S,
            url,
        )
        return (
            replace(
                result,
                error=(
                    f"browser context close exceeded {CONTEXT_CLOSE_TIMEOUT_S}s "
                    f"budget; http archive discarded"
                ),
            ),
            None,
        )
    stages["close"] = time.monotonic() - stage_start

    # The HAR is written when the context closes. Unpack it only when
    # navigation committed — otherwise it holds no useful evidence. Bodies are
    # stored under final_url's hostname prefix so every artifact shares the
    # one prefix exposed in the API response; the manifest itself is returned
    # to persist inline as JSON.
    if result.http_status is None or not http_archive_path.exists():
        return result, None

    http_archive: dict | None = None
    stage_start = time.monotonic()
    try:
        async with asyncio.timeout(HAR_PROCESSING_TIMEOUT_S):
            http_archive = await capture_http_archive(
                http_archive_path, result.final_url, download=result.download
            )
    except asyncio.TimeoutError:
        stages["har"] = time.monotonic() - stage_start
        logger.error(
            "HAR processing exceeded %ss budget for %s; discarding HAR",
            HAR_PROCESSING_TIMEOUT_S,
            url,
        )
        return (
            replace(
                result,
                error=(
                    f"HAR processing exceeded {HAR_PROCESSING_TIMEOUT_S}s "
                    f"budget; http archive discarded"
                ),
            ),
            None,
        )
    stages["har"] = time.monotonic() - stage_start
    return result, http_archive


async def _close_browser(browser: Browser) -> None:
    """Force-close a connected browser under a bounded timeout.

    ``browser.close()`` clears contexts and disconnects from the remote server
    (analogous to force-quitting); it has no timeout argument, so bound it.
    Cleanup runs after the snapshot is finalized, so a timeout or error here
    is logged as an operational warning only and never alters the finalized
    snapshot's success or error state.
    """
    try:
        async with asyncio.timeout(BROWSER_CLOSE_TIMEOUT_S):
            await browser.close()
    except asyncio.TimeoutError:
        logger.warning(
            "browser.close exceeded %ss budget; cleanup abandoned",
            BROWSER_CLOSE_TIMEOUT_S,
        )
    except PlaywrightError as error:
        logger.warning("browser.close failed: %s", error.message)


def _build_snapshot(
    body: SnapshotCreate,
    result: CaptureResult,
    http_archive: dict | None,
) -> Snapshot:
    """Map captured evidence onto a persistable ``Snapshot`` row."""
    return Snapshot(
        url=str(body.url),
        final_url=result.final_url,
        http_status=result.http_status,
        error=result.error,
        condition_type=body.condition_type,
        condition=body.condition,
        condition_met=result.condition_met,
        plaintext=result.plaintext,
        rendered_html=result.rendered_html,
        screenshot=result.screenshot,
        http_archive=http_archive,
    )
