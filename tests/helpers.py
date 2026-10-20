"""Shared helpers for browser routing and reading persisted state."""

import base64
import uuid
from pathlib import Path

from sqlalchemy import select

from pravda.db import SnapshotRecord

FIXTURES = Path(__file__).parent / "fixtures"

# A valid 1x1 PNG, so subresource routes record real image bytes.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBg"
    "AAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


def fulfill_html(route):
    """Fulfill a route with the example HTML fixture."""
    return route.fulfill(
        body=(FIXTURES / "example.html").read_text(),
        headers={"content-type": "text/html"},
    )


def fulfill_pdf(route):
    """Fulfill a route with the sample PDF fixture."""
    return route.fulfill(
        body=(FIXTURES / "sample.pdf").read_bytes(),
        headers={"content-type": "application/pdf"},
    )


def fulfill_standalone(route):
    """Fulfill a route with the standalone HTML fixture."""
    return route.fulfill(
        body=(FIXTURES / "standalone.html").read_text(),
        headers={"content-type": "text/html"},
    )


async def snapshot_ids(sessionmaker, url: str) -> set[uuid.UUID]:
    """Persisted snapshot ids for a URL, read the way applications do."""
    async with sessionmaker() as session:
        result = await session.execute(
            select(SnapshotRecord.id).where(SnapshotRecord.url == url)
        )
        return set(result.scalars().all())
