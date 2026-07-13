"""Direct library tests for the history API and public Snapshot dataclass.

Unlike the HTTP tests (which ride a rolled-back transaction through the
dependency-injected session), these exercise ``pravda.snapshots()`` against
rows committed through Pravda's own session factory — the real consumption
path for a downstream caller. Those commits are not rolled back, so each test
cleans up the rows it introduced to keep the shared test database isolated.
"""

import dataclasses
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

import pravda
from pravda import Snapshot, snapshots
from pravda.db import ConditionType, SnapshotRecord, async_session


async def _commit_snapshot(
    url: str,
    captured_at: datetime,
    *,
    final_url: str | None = None,
    http_status: int | None = 200,
    rendered_html: str | None = None,
    condition_met: bool = True,
) -> uuid.UUID:
    """Insert and commit a snapshot via Pravda's own session factory."""
    record = SnapshotRecord(
        id=uuid.uuid4(),
        url=url,
        captured_at=captured_at,
        http_status=http_status,
        final_url=final_url,
        condition_type=ConditionType.lifecycle,
        condition="load",
        condition_met=condition_met,
        rendered_html=rendered_html,
    )
    async with async_session() as session:
        session.add(record)
        await session.commit()
    return record.id


@pytest.fixture(autouse=True)
async def _cleanup_committed_rows(db_engine):
    """Remove every committed snapshot row after each test.

    Rows inserted through ``async_session`` are real commits to the shared
    test database (not part of any rolled-back transaction), so they survive
    unless deleted explicitly. No other test commits rows, so a blanket
    delete keeps things simple and isolated.
    """
    yield
    async with async_session() as session:
        await session.execute(delete(SnapshotRecord))
        await session.commit()


@pytest.mark.asyncio
async def test_snapshots_returns_exact_url_matches_newest_first():
    url = "https://example.com"
    older = await _commit_snapshot(url, datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = await _commit_snapshot(url, datetime(2026, 1, 2, tzinfo=timezone.utc))
    # A different URL must not appear in the results.
    await _commit_snapshot(
        "https://different.example", datetime(2026, 1, 3, tzinfo=timezone.utc)
    )

    results = await snapshots(url)

    assert [snapshot.id for snapshot in results] == [newer, older]


@pytest.mark.asyncio
async def test_snapshots_empty_for_unknown_url():
    assert await snapshots("https://nope.example") == []


@pytest.mark.asyncio
async def test_snapshots_returns_public_dataclass_with_resolved_prefix():
    url = "https://example.com"
    await _commit_snapshot(
        url,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        final_url="https://example.com/page",
        rendered_html="a" * 40 + ".html",
    )

    result = (await snapshots(url))[0]

    assert isinstance(result, Snapshot)
    # All fields of the ORM row are carried through.
    assert result.url == url
    assert result.final_url == "https://example.com/page"
    assert result.http_status == 200
    assert result.condition_type is ConditionType.lifecycle
    assert result.condition_met is True
    assert result.rendered_html == "a" * 40 + ".html"
    # prefix is resolved from final_url (base path + normalized hostname).
    assert result.prefix is not None
    assert result.prefix.endswith("example.com")


@pytest.mark.asyncio
async def test_snapshot_prefix_is_none_when_navigation_never_committed():
    url = "https://example.com"
    # No final_url: navigation never committed, no artifacts stored.
    await _commit_snapshot(
        url,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        final_url=None,
        http_status=None,
        condition_met=False,
    )

    result = (await snapshots(url))[0]

    assert result.final_url is None
    assert result.prefix is None
    assert result.http_status is None


@pytest.mark.asyncio
async def test_snapshot_is_immutable():
    url = "https://example.com"
    await _commit_snapshot(url, datetime(2026, 1, 1, tzinfo=timezone.utc))

    result = (await snapshots(url))[0]

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.url = "https://mutated.example"


@pytest.mark.asyncio
async def test_snapshots_reads_committed_rows_through_pravda_session(
    db_session: AsyncSession,
):
    """A row visible only after a real commit is returned by snapshots().

    The ``db_session`` fixture rolls back, so a row added there but not
    committed is invisible to ``snapshots()`` (which uses its own session).
    Committing an independent row through ``async_session`` makes it visible —
    proving the library reads committed state through Pravda's own factory.
    """
    url = "https://example.com"

    # This row is never committed; it must not be returned.
    db_session.add(
        SnapshotRecord(
            id=uuid.uuid4(),
            url=url,
            captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            condition_type=ConditionType.lifecycle,
            condition="load",
            condition_met=True,
        )
    )
    await db_session.flush()

    committed_id = await _commit_snapshot(
        url, datetime(2026, 1, 2, tzinfo=timezone.utc)
    )

    results = await snapshots(url)

    assert [snapshot.id for snapshot in results] == [committed_id]


def test_pravda_exports_public_names():
    assert pravda.Snapshot is Snapshot
    assert callable(pravda.snapshots)
