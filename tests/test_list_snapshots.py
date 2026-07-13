import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pravda.api import app
from pravda.db import ConditionType, SnapshotRecord


@pytest.fixture()
async def client(db_session: AsyncSession):
    """HTTP client wired to the test session via dependency override."""
    from pravda.db import get_session

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    app.dependency_overrides.clear()


async def _insert_snapshot(
    db: AsyncSession, url: str, captured_at: datetime, http_status: int = 200
) -> SnapshotRecord:
    snapshot = SnapshotRecord(
        id=uuid.uuid4(),
        url=url,
        captured_at=captured_at,
        http_status=http_status,
        condition_type=ConditionType.lifecycle,
        condition="load",
        condition_met=True,
    )
    snapshot.rendered_html = "a" * 64
    db.add(snapshot)
    await db.flush()
    return snapshot


@pytest.mark.asyncio
async def test_list_snapshots_returns_matching_url(client, db_session):
    url = "https://example.com"
    await _insert_snapshot(db_session, url, datetime(2026, 1, 1, tzinfo=timezone.utc))
    await _insert_snapshot(db_session, url, datetime(2026, 1, 2, tzinfo=timezone.utc))
    # Different URL — should not appear
    await _insert_snapshot(
        db_session, "https://other.com", datetime(2026, 1, 3, tzinfo=timezone.utc)
    )

    resp = await client.get("/snapshots", params={"url": url})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    # Ordered by captured_at desc
    assert body["items"][0]["captured_at"] > body["items"][1]["captured_at"]


@pytest.mark.asyncio
async def test_list_snapshots_pagination(client, db_session):
    url = "https://example.com"
    for i in range(12):
        await _insert_snapshot(
            db_session, url, datetime(2026, 1, i + 1, tzinfo=timezone.utc)
        )

    resp_page1 = await client.get("/snapshots", params={"url": url, "page": 1})
    resp_page2 = await client.get("/snapshots", params={"url": url, "page": 2})

    body1 = resp_page1.json()
    body2 = resp_page2.json()

    assert body1["total"] == 12
    assert len(body1["items"]) == 10  # PAGE_SIZE = 10
    assert len(body2["items"]) == 2
