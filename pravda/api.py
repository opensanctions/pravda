import json
import logging
import os
import uuid

from fastapi import Depends, FastAPI, Query
from playwright.async_api import async_playwright
from pydantic import BaseModel, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pravda.capture import capture_page
from pravda.db import Snapshot, get_session, init_db
from pravda.storage import content_path

BROWSER_CHANNEL = "chrome"
BROWSER_WS_URL = os.environ["BROWSER_WS_URL"]

logger = logging.getLogger(__name__)

app = FastAPI(title="Pravda", description="Evidence layer for web pages")


@app.on_event("startup")
async def startup() -> None:
    await init_db()
    logger.info("Database initialized")


# --- Request / response models ---


class SnapshotCreate(BaseModel):
    url: HttpUrl


class ContentOut(BaseModel):
    content_type: str
    path: str


class HeaderOut(BaseModel):
    name: str
    value: str


class SnapshotOut(BaseModel):
    id: uuid.UUID
    url: str
    captured_at: str
    http_status: int
    contents: list[ContentOut]
    headers: list[HeaderOut]


class SnapshotsOut(BaseModel):
    items: list[SnapshotOut]
    total: int


class HealthOut(BaseModel):
    status: str


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
        items=[
            SnapshotOut(
                id=s.id,
                url=s.url,
                captured_at=s.captured_at.isoformat(),
                http_status=s.http_status,
                contents=[
                    ContentOut(content_type=c.content_type, path=content_path(c.hash))
                    for c in s.contents
                ],
                headers=[HeaderOut(name=h.name, value=h.value) for h in s.headers],
            )
            for s in rows
        ],
        total=total,
    )


@app.post("/snapshots", response_model=SnapshotOut)
async def create_snapshot(
    body: SnapshotCreate,
    session: AsyncSession = Depends(get_session),
) -> SnapshotOut:
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

        snapshot = await capture_page(page, str(body.url), session)

        await context.close()

    await session.commit()
    return SnapshotOut(
        id=snapshot.id,
        url=snapshot.url,
        captured_at=snapshot.captured_at.isoformat(),
        http_status=snapshot.http_status,
        contents=[
            ContentOut(content_type=c.content_type, path=content_path(c.hash))
            for c in snapshot.contents
        ],
        headers=[HeaderOut(name=h.name, value=h.value) for h in snapshot.headers],
    )
