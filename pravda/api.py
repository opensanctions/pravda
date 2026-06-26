import json
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Query
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field, HttpUrl, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pravda.capture import CaptureResult, capture_page
from pravda.db import ConditionType, ResponseBody, Snapshot, get_session, init_db
from pravda.http_archive import capture_http_archive
from pravda.storage import content_path

BROWSER_CHANNEL = "chrome"
BROWSER_WS_URL = os.environ["BROWSER_WS_URL"]

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        filename="pravda.log",
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    await init_db()
    logger.info("Database initialized")
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

    `plaintext`, `rendered_html`, and `screenshot` are content-addressed
    storage locations (a filename of the form ``<sha1>.<extension>``) under
    the shared storage backend. Downstream services with access to that backend
    read the files directly from the returned location — there is no blob
    download endpoint. Each is null when that artifact was not captured (e.g.
    the page never committed, or the capture timed out). The file extension
    carries the artifact's type (txt, html, png). `http_archive` is the
    content-addressed storage location of the recorded HAR (``.har``) —
    metadata only, with each entry's ``content._file`` pointing at a body
    stored under its own content-addressed location. `response_bodies` lists those
    body locations. `http_archive` is null when navigation never committed.
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
    plaintext: str | None = Field(
        default=None,
        description=(
            "Content-addressed storage location of the page text (``.txt``), or null"
        ),
    )
    rendered_html: str | None = Field(
        default=None,
        description=(
            "Content-addressed storage location of the rendered HTML "
            "(``.html``), or null"
        ),
    )
    screenshot: str | None = Field(
        default=None,
        description=(
            "Content-addressed storage location of the full-page screenshot "
            "(``.png``), or null"
        ),
    )
    http_archive: str | None = Field(
        default=None,
        description=(
            "Content-addressed storage location of the recorded HAR "
            "(``.har``; metadata only), or null"
        ),
    )
    response_bodies: dict[str, str] = Field(
        description=(
            "Response bodies recorded in the page's HAR, keyed by their "
            "content-addressed filename and mapping to their storage path"
        )
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
        .options(selectinload(Snapshot.response_bodies))
    )
    rows = (await session.execute(rows_stmt)).scalars().all()

    return SnapshotsOut(
        items=[_snapshot_out(row) for row in rows],
        total=total,
    )


def _snapshot_out(snapshot: Snapshot) -> SnapshotOut:
    prefix_url = snapshot.final_url or snapshot.url
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
        plaintext=content_path(prefix_url, snapshot.plaintext)
        if snapshot.plaintext
        else None,
        rendered_html=(
            content_path(prefix_url, snapshot.rendered_html)
            if snapshot.rendered_html
            else None
        ),
        screenshot=(
            content_path(prefix_url, snapshot.screenshot)
            if snapshot.screenshot
            else None
        ),
        http_archive=content_path(prefix_url, snapshot.http_archive)
        if snapshot.http_archive
        else None,
        response_bodies={
            body.file: content_path(prefix_url, body.file)
            for body in snapshot.response_bodies
        },
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
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect(
                BROWSER_WS_URL,
                headers={
                    "x-playwright-launch-options": json.dumps(
                        {"channel": BROWSER_CHANNEL, "headless": False}
                    ),
                },
            )
            # Record a HAR of all network activity, with response bodies
            # stored as separate entries inside a zip archive
            # (record_har_content="attach" + a .zip path). The file is flushed
            # when the context closes.
            http_archive_path = http_archive_dir / "record.zip"
            context = await browser.new_context(
                record_har_path=str(http_archive_path),
                record_har_content="attach",
            )
            page = await context.new_page()

            result = await capture_page(
                page,
                str(body.url),
                condition_type=body.condition_type,
                condition=body.condition,
            )

            await context.close()

            # The HAR is written when the context closes. Unpack it only when
            # navigation committed — otherwise it holds no useful evidence.
            http_archive_capture = None
            if result.http_status is not None and http_archive_path.exists():
                http_archive_capture = await capture_http_archive(
                    http_archive_path, str(body.url)
                )
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
        )
        http_archive_capture = None
    finally:
        shutil.rmtree(http_archive_dir, ignore_errors=True)

    snapshot = _build_snapshot(body, result, http_archive_capture)
    session.add(snapshot)
    await session.commit()
    logger.info(
        "Captured %s id=%s: status=%s condition_met=%s"
        " http_archive=%s response_bodies=%d error=%s",
        snapshot.url,
        snapshot.id,
        snapshot.http_status,
        snapshot.condition_met,
        snapshot.http_archive is not None,
        len(snapshot.response_bodies),
        snapshot.error,
    )
    return _snapshot_out(snapshot)


def _build_snapshot(
    body: SnapshotCreate,
    result: CaptureResult,
    http_archive_capture,
) -> Snapshot:
    """Map captured evidence onto a persistable ``Snapshot`` row."""
    snapshot = Snapshot(
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
        http_archive=http_archive_capture.http_archive
        if http_archive_capture
        else None,
    )
    if http_archive_capture:
        snapshot.response_bodies = [
            ResponseBody(file=file) for file in http_archive_capture.response_bodies
        ]
    return snapshot
