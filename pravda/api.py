"""HTTP API adapter for Pravda.

The capture orchestration lives in the library (``pravda.snapshot``,
``pravda.browser``). This module is a thin FastAPI surface over it: the
``POST /snapshots`` endpoint delegates to :func:`pravda.snapshot`, and the
``GET /snapshots`` history endpoint reads through the dependency-injected
session. Removing the HTTP layer (FastAPI, deployment, docs) is a later run;
for now the API stays operational as an adapter.
"""

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import pravda
from pravda.db import SnapshotRecord, get_session
from pravda.snapshots import Snapshot, from_record

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
    total_stmt = (
        select(func.count())
        .select_from(SnapshotRecord)
        .where(SnapshotRecord.url == url)
    )
    total = (await session.execute(total_stmt)).scalar_one()

    rows_stmt = (
        select(SnapshotRecord)
        .where(SnapshotRecord.url == url)
        .order_by(SnapshotRecord.captured_at.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    rows = (await session.execute(rows_stmt)).scalars().all()

    return SnapshotsOut(
        items=[_snapshot_out_from_record(row) for row in rows],
        total=total,
    )


def _snapshot_out_from_record(record: SnapshotRecord) -> SnapshotOut:
    """Map a persisted row to the HTTP response model via the public value."""
    return _snapshot_out(from_record(record))


def _snapshot_out(snapshot: Snapshot) -> SnapshotOut:
    return SnapshotOut(
        id=snapshot.id,
        url=snapshot.url,
        final_url=snapshot.final_url,
        captured_at=snapshot.captured_at,
        http_status=snapshot.http_status,
        error=snapshot.error,
        prefix=snapshot.prefix,
        plaintext=snapshot.plaintext,
        rendered_html=snapshot.rendered_html,
        screenshot=snapshot.screenshot,
        http_archive=snapshot.http_archive,
    )


@app.post("/snapshots", response_model=SnapshotOut)
async def create_snapshot(body: SnapshotCreate) -> SnapshotOut:
    """Capture a snapshot by delegating to the library one-shot path.

    The library owns the browser and the database session (it commits through
    Pravda's own session factory), so this endpoint takes no injected session.
    """
    logger.info("POST /snapshots %s", body.url)
    snapshot = await pravda.snapshot(str(body.url))
    return _snapshot_out(snapshot)
