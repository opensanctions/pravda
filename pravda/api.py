import json
import logging
import os
import uuid

from fastapi import Depends, FastAPI, HTTPException
from playwright.async_api import async_playwright
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pravda.capture import capture_page
from pravda.db import Snapshot, get_session, init_db

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
    url: str


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


class HealthOut(BaseModel):
    status: str


class SnapshotCreated(BaseModel):
    id: uuid.UUID


# --- Endpoints ---


@app.get("/health")
async def health() -> HealthOut:
    return HealthOut(status="ok")


@app.post("/snapshots", response_model=SnapshotCreated)
async def create_snapshot(
    body: SnapshotCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
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

        snapshot = await capture_page(page, body.url, session)

        await context.close()

    await session.commit()
    return {"id": snapshot.id}


@app.get("/snapshots/{snapshot_id}")
async def get_snapshot(
    snapshot_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> SnapshotOut:
    stmt = (
        select(Snapshot)
        .where(Snapshot.id == snapshot_id)
        .options(selectinload(Snapshot.contents), selectinload(Snapshot.headers))
    )
    result = await session.execute(stmt)
    snapshot = result.scalar_one_or_none()

    if snapshot is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    return SnapshotOut(
        id=snapshot.id,
        url=snapshot.url,
        captured_at=snapshot.captured_at.isoformat(),
        http_status=snapshot.http_status,
        contents=[
            ContentOut(content_type=c.content_type, path=c.hash)
            for c in snapshot.contents
        ],
        headers=[HeaderOut(name=h.name, value=h.value) for h in snapshot.headers],
    )
