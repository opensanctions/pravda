import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field, HttpUrl, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pravda.capture import capture_page
from pravda.db import ConditionType, Snapshot, get_session, init_db
from pravda.storage import content_path

LifecycleWait = Literal["load", "domcontentloaded", "networkidle", "commit"]

BROWSER_CHANNEL = "chrome"
BROWSER_WS_URL = os.environ["BROWSER_WS_URL"]

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
                "Depends on condition_type: for 'lifecycle' one of "
                "'load', 'domcontentloaded', 'networkidle', 'commit'. "
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


class ContentOut(BaseModel):
    content_type: str = Field(description="MIME type of the captured content")
    path: str = Field(description="File path where the content is stored")


class HeaderOut(BaseModel):
    name: str = Field(description="HTTP header name (lowercased)")
    value: str = Field(description="HTTP header value")


class SnapshotOut(BaseModel):
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
    lifecycle_events: list[str] = Field(
        description=(
            "CDP lifecycle events that fired during navigation, "
            "in chronological order (e.g. init, commit, DOMContentLoaded, "
            "firstPaint, firstContentfulPaint, load)."
        ),
    )
    contents: list[ContentOut] = Field(description="Captured content files")
    headers: list[HeaderOut] = Field(description="Response headers from the page")


class SnapshotsOut(BaseModel):
    items: list[SnapshotOut]
    total: int = Field(description="Total number of snapshots for this URL")


class HealthOut(BaseModel):
    status: str = Field(description="Service health status")


# --- Endpoints ---


PAGE_SIZE = 10


@app.get("/health")
async def health() -> HealthOut:
    return HealthOut(status="ok")


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
        .options(selectinload(Snapshot.contents), selectinload(Snapshot.headers))
    )
    rows = (await session.execute(rows_stmt)).scalars().all()

    return SnapshotsOut(
        items=[_snapshot_out(s) for s in rows],
        total=total,
    )


def _snapshot_out(s: Snapshot) -> SnapshotOut:
    return SnapshotOut(
        id=s.id,
        url=s.url,
        captured_at=s.captured_at,
        http_status=s.http_status,
        error=s.error,
        condition_type=s.condition_type,
        condition=s.condition,
        condition_met=s.condition_met,
        lifecycle_events=s.lifecycle_events or [],
        contents=[
            ContentOut(content_type=c.content_type, path=content_path(c.hash))
            for c in s.contents
        ],
        headers=[HeaderOut(name=h.name, value=h.value) for h in s.headers],
    )


@app.post("/snapshots", response_model=SnapshotOut)
async def create_snapshot(
    body: SnapshotCreate,
    session: AsyncSession = Depends(get_session),
) -> SnapshotOut:
    error: str | None = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect(
                BROWSER_WS_URL,
                headers={
                    "x-playwright-launch-options": json.dumps(
                        {"channel": BROWSER_CHANNEL, "headless": False}
                    ),
                },
            )
            context = await browser.new_context()
            page = await context.new_page()

            snapshot = await capture_page(
                page,
                str(body.url),
                session,
                condition_type=body.condition_type,
                condition=body.condition,
            )

            await context.close()
    except PlaywrightError as e:
        error = e.message
        snapshot = Snapshot(
            url=str(body.url),
            http_status=None,
            error=error,
            condition_type=body.condition_type,
            condition=body.condition,
            condition_met=False,
            lifecycle_events=[],
            contents=[],
            headers=[],
        )
        session.add(snapshot)

    await session.commit()
    return _snapshot_out(snapshot)
