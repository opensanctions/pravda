import logging
import uuid

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from pravda.constants import BROWSER_CHANNEL, BROWSER_WS_URL
from pravda.db import Content, Header, Snapshot, get_session, init_db
from pravda.storage import put_blob

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
    hash: str


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


# --- Endpoints ---


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/snapshots")
async def create_snapshot(
    body: SnapshotCreate,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    from playwright.async_api import async_playwright

    # 1. Connect to browser, capture page
    async with async_playwright() as p:
        browser = await p.chromium.connect(
            BROWSER_WS_URL,
            headers={
                "x-playwright-launch-options": f'{{"channel": "{BROWSER_CHANNEL}"}}'
            },
        )
        context = await browser.new_context()
        page = await context.new_page()

        response = await page.goto(body.url, wait_until="networkidle")
        http_status = response.status if response else 0

        # Collect headers
        resp_headers: dict[str, str] = {}
        if response:
            raw = await response.all_headers()
            resp_headers = {k.lower(): v for k, v in raw.items()}

        mhtml_bytes = (await page.context.storage_state()).encode()  # fallback
        mhtml_bytes = await page.content()
        mhtml_bytes = mhtml_bytes.encode("utf-8")

        screenshot_bytes = await page.screenshot(full_page=True)

        await context.close()

    # 2. Store blobs
    mhtml_hash = await put_blob(mhtml_bytes)
    screenshot_hash = await put_blob(screenshot_bytes)

    # 3. Persist snapshot row
    snapshot = Snapshot(url=body.url, http_status=http_status)
    session.add(snapshot)
    await session.flush()

    session.add(Content(snapshot_id=snapshot.id, content_type="mhtml", hash=mhtml_hash))
    session.add(
        Content(
            snapshot_id=snapshot.id,
            content_type="screenshot",
            hash=screenshot_hash,
        )
    )

    for name, value in resp_headers.items():
        session.add(Header(snapshot_id=snapshot.id, name=name, value=value))

    await session.commit()
    logger.info("Saved snapshot %s for %s", snapshot.id, body.url)

    return {"id": str(snapshot.id)}


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
            ContentOut(content_type=c.content_type, hash=c.hash)
            for c in snapshot.contents
        ],
        headers=[HeaderOut(name=h.name, value=h.value) for h in snapshot.headers],
    )
