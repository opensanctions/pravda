import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Query
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field, HttpUrl, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pravda.capture import CaptureResult, capture_page
from pravda.db import ConditionType, Header, Snapshot, get_session, init_db
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
                "one of 'load', 'DOMContentLoaded', 'networkIdle', 'commit'. "
                "For 'selector', a CSS selector to wait for."
            )
        ),
    ] = "load"

    @model_validator(mode="after")
    def _validate_condition(self) -> "SnapshotCreate":
        if self.condition_type is ConditionType.lifecycle:
            valid = {"load", "DOMContentLoaded", "networkIdle", "commit"}
            if self.condition not in valid:
                raise ValueError(
                    f"condition must be one of {valid} "
                    f"when condition_type is 'lifecycle', "
                    f"got '{self.condition}'"
                )
        return self


class HeaderOut(BaseModel):
    name: str = Field(description="HTTP header name (lowercased)")
    value: str = Field(description="HTTP header value")


class SnapshotOut(BaseModel):
    """A captured snapshot of a web page.

    `plaintext`, `rendered_html`, `screenshot`, and `blob` are content-addressed
    storage locations (a SHA-256 hex digest as the filename) under the shared
    storage backend. Downstream services with access to that backend read the
    files directly from the returned location — there is no blob download
    endpoint. Each is null when that artifact was not captured (e.g. the
    page never committed, or the capture timed out).
    """

    id: uuid.UUID
    url: HttpUrl = Field(description="The URL that was captured")
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
            "Content-addressed storage location of the page text (text/plain), or null"
        ),
    )
    rendered_html: str | None = Field(
        default=None,
        description=(
            "Content-addressed storage location of the rendered HTML "
            "(text/html), or null"
        ),
    )
    screenshot: str | None = Field(
        default=None,
        description=(
            "Content-addressed storage location of the full-page screenshot "
            "(image/png), or null"
        ),
    )
    blob: str | None = Field(
        default=None,
        description=(
            "Content-addressed storage location of the archive blob, or null. "
            "Its MIME type is in `blob_content_type`."
        ),
    )
    blob_content_type: str | None = Field(
        default=None,
        description="MIME type of the blob, or null when no blob was captured",
    )
    headers: list[HeaderOut] = Field(description="Response headers from the page")


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
        .options(selectinload(Snapshot.headers))
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
        captured_at=snapshot.captured_at,
        http_status=snapshot.http_status,
        error=snapshot.error,
        condition_type=snapshot.condition_type,
        condition=snapshot.condition,
        condition_met=snapshot.condition_met,
        plaintext=content_path(snapshot.plaintext) if snapshot.plaintext else None,
        rendered_html=(
            content_path(snapshot.rendered_html) if snapshot.rendered_html else None
        ),
        screenshot=content_path(snapshot.screenshot) if snapshot.screenshot else None,
        blob=content_path(snapshot.blob) if snapshot.blob else None,
        blob_content_type=snapshot.blob_content_type,
        headers=[
            HeaderOut(name=header.name, value=header.value)
            for header in snapshot.headers
        ],
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
            context = await browser.new_context()
            page = await context.new_page()

            result = await capture_page(
                page,
                str(body.url),
                condition_type=body.condition_type,
                condition=body.condition,
            )

            await context.close()
    except PlaywrightError as error:
        # Couldn't even reach the browser — record an empty, failed result.
        logger.error("Browser error for %s: %s", body.url, error.message)
        result = CaptureResult(
            http_status=None,
            error=error.message,
            condition_met=False,
            headers={},
            plaintext_hash=None,
            rendered_html_hash=None,
            screenshot_hash=None,
            blob_hash=None,
            blob_content_type=None,
        )

    snapshot = _build_snapshot(body, result)
    session.add(snapshot)
    await session.commit()
    artifacts = (
        snapshot.plaintext,
        snapshot.rendered_html,
        snapshot.screenshot,
        snapshot.blob,
    )
    logger.info(
        "Captured %s id=%s: status=%s condition_met=%s artifacts=%d error=%s",
        snapshot.url,
        snapshot.id,
        snapshot.http_status,
        snapshot.condition_met,
        sum(hash is not None for hash in artifacts),
        snapshot.error,
    )
    return _snapshot_out(snapshot)


def _build_snapshot(body: SnapshotCreate, result: CaptureResult) -> Snapshot:
    """Map captured evidence onto a persistable ``Snapshot`` row."""
    snapshot = Snapshot(
        url=str(body.url),
        http_status=result.http_status,
        error=result.error,
        condition_type=body.condition_type,
        condition=body.condition,
        condition_met=result.condition_met,
        plaintext=result.plaintext_hash,
        rendered_html=result.rendered_html_hash,
        screenshot=result.screenshot_hash,
        blob=result.blob_hash,
        blob_content_type=result.blob_content_type,
    )
    snapshot.headers = [
        Header(name=name, value=value) for name, value in result.headers.items()
    ]
    return snapshot
