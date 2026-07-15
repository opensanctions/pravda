"""Direct library tests for ``Pravda.snapshots()`` and the Snapshot dataclass."""

import dataclasses
import uuid
from datetime import datetime, timezone

import pytest

import pravda as pravda_pkg
from pravda import Pravda, Snapshot
from pravda.db import SnapshotRecord


async def _commit_snapshot(
    database,
    url: str,
    captured_at: datetime,
    *,
    final_url: str | None = None,
    http_status: int | None = 200,
    rendered_html: str | None = None,
    http_archive: dict | None = None,
) -> uuid.UUID:
    """Insert and commit a snapshot through the test database fixture."""
    record = SnapshotRecord(
        id=uuid.uuid4(),
        url=url,
        captured_at=captured_at,
        http_status=http_status,
        final_url=final_url,
        rendered_html=rendered_html,
        http_archive=http_archive,
    )
    async with database() as session:
        session.add(record)
        await session.commit()
    return record.id


@pytest.mark.asyncio
async def test_snapshots_returns_exact_url_matches_newest_first(
    pravda: Pravda, database
):
    url = "https://example.com"
    older = await _commit_snapshot(
        database, url, datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    newer = await _commit_snapshot(
        database, url, datetime(2026, 1, 2, tzinfo=timezone.utc)
    )
    # A different URL must not appear in the results.
    await _commit_snapshot(
        database,
        "https://different.example",
        datetime(2026, 1, 3, tzinfo=timezone.utc),
    )

    results = await pravda.snapshots(url)

    assert [snapshot.id for snapshot in results] == [newer, older]


@pytest.mark.asyncio
async def test_snapshots_returns_public_dataclass_with_resolved_prefix(
    pravda: Pravda, database
):
    url = "https://example.com"
    await _commit_snapshot(
        database,
        url,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        final_url="https://example.com/page",
        rendered_html="a" * 40 + ".html",
    )

    result = (await pravda.snapshots(url))[0]

    assert isinstance(result, Snapshot)
    assert result.url == url
    assert result.final_url == "https://example.com/page"
    assert result.http_status == 200
    assert result.rendered_html == "a" * 40 + ".html"
    # prefix is resolved from final_url via normalized hostname.
    assert result.prefix is not None
    assert result.prefix.endswith("example.com")


@pytest.mark.asyncio
async def test_snapshot_prefix_is_none_when_navigation_never_committed(
    pravda: Pravda, database
):
    url = "https://example.com"
    # No final_url: navigation never committed, no artifacts stored.
    await _commit_snapshot(
        database,
        url,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        final_url=None,
        http_status=None,
    )

    result = (await pravda.snapshots(url))[0]

    assert result.final_url is None
    assert result.prefix is None
    assert result.http_status is None


@pytest.mark.asyncio
async def test_snapshot_is_immutable(pravda: Pravda, database):
    url = "https://example.com"
    await _commit_snapshot(database, url, datetime(2026, 1, 1, tzinfo=timezone.utc))

    result = (await pravda.snapshots(url))[0]

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.url = "https://mutated.example"


def test_pravda_exports_public_names():
    assert pravda_pkg.Pravda is Pravda
    assert pravda_pkg.Snapshot is Snapshot
    assert callable(pravda_pkg.PravdaConfig)
