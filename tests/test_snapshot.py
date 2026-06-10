import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from pravda.db import Content, Header, Snapshot


@pytest.mark.asyncio
async def test_create_and_query_snapshot(db_session):
    """Round-trip: insert snapshot + contents + headers, then read them back."""
    snapshot = Snapshot(url="https://example.com", http_status=200)
    db_session.add(snapshot)
    await db_session.flush()

    db_session.add(
        Content(snapshot_id=snapshot.id, content_type="mhtml", hash="a" * 64)
    )
    db_session.add(
        Content(snapshot_id=snapshot.id, content_type="screenshot", hash="b" * 64)
    )
    db_session.add(
        Header(snapshot_id=snapshot.id, name="content-type", value="text/html")
    )

    await db_session.flush()

    # Read back
    stmt = (
        select(Snapshot)
        .where(Snapshot.id == snapshot.id)
        .options(selectinload(Snapshot.contents), selectinload(Snapshot.headers))
    )
    result = await db_session.execute(stmt)
    loaded = result.scalar_one()

    assert loaded.url == "https://example.com"
    assert loaded.http_status == 200
    assert {c.content_type for c in loaded.contents} == {"mhtml", "screenshot"}
    assert loaded.headers[0].name == "content-type"
    assert loaded.headers[0].value == "text/html"
