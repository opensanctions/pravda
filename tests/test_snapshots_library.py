"""Direct library tests for ``Pravda.snapshots()`` and the Snapshot dataclass."""

import dataclasses
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import pravda as pravda_pkg
from pravda import Pravda, Snapshot
from pravda.db import SnapshotRecord
from pravda.snapshots import from_record
from pravda.storage import Storage


async def _commit_snapshot(
    database,
    url: str,
    captured_at: datetime,
    *,
    final_url: str | None = None,
    http_status: int | None = 200,
    plaintext: str | None = None,
    rendered_html: str | None = None,
    screenshot: str | None = None,
    http_archive: dict | None = None,
) -> uuid.UUID:
    """Insert and commit a snapshot through the test database fixture."""
    record = SnapshotRecord(
        id=uuid.uuid4(),
        url=url,
        captured_at=captured_at,
        http_status=http_status,
        final_url=final_url,
        plaintext=plaintext,
        rendered_html=rendered_html,
        screenshot=screenshot,
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
async def test_snapshots_returns_public_dataclass_with_resolved_paths(
    pravda: Pravda, database, tmp_path
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
    prefix = os.path.join(str(tmp_path), "example.com")
    assert result.rendered_html == os.path.join(prefix, "a" * 40 + ".html")
    assert not hasattr(result, "prefix")


@pytest.mark.asyncio
async def test_snapshot_artifacts_are_none_when_navigation_never_committed(
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
    assert result.http_status is None
    assert result.plaintext is None
    assert result.rendered_html is None
    assert result.screenshot is None
    assert result.http_archive is None


@pytest.mark.asyncio
async def test_from_record_resolves_paths_without_mutating_persisted_record(
    database, tmp_path
):
    """The public mapping resolves artifact paths but keeps the record relative."""
    http_archive = {
        "log": {
            "entries": [
                {
                    "request": {"method": "GET", "url": "https://example.com/"},
                    "response": {
                        "status": 200,
                        "content": {"mimeType": "text/html", "_file": "abc.html"},
                    },
                },
                {
                    "request": {"method": "GET", "url": "https://example.com/img"},
                    "response": {"status": 200, "content": {"mimeType": "image/png"}},
                },
            ]
        }
    }
    await _commit_snapshot(
        database,
        "https://example.com",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        final_url="https://example.com/page",
        plaintext="aaa.txt",
        rendered_html="bbb.html",
        screenshot="ccc.png",
        http_archive=http_archive,
    )

    storage = Storage.from_url(str(tmp_path))
    async with database() as session:
        record = (
            await session.execute(
                select(SnapshotRecord).where(
                    SnapshotRecord.url == "https://example.com"
                )
            )
        ).scalar_one()
        snapshot = from_record(record, storage)

    prefix = os.path.join(str(tmp_path), "example.com")
    assert snapshot.plaintext == os.path.join(prefix, "aaa.txt")
    assert snapshot.rendered_html == os.path.join(prefix, "bbb.html")
    assert snapshot.screenshot == os.path.join(prefix, "ccc.png")
    entries = snapshot.http_archive["log"]["entries"]
    assert entries[0]["response"]["content"]["_file"] == os.path.join(
        prefix, "abc.html"
    )
    assert "_file" not in entries[1]["response"]["content"]

    assert record.plaintext == "aaa.txt"
    assert record.rendered_html == "bbb.html"
    assert record.screenshot == "ccc.png"
    assert (
        record.http_archive["log"]["entries"][0]["response"]["content"]["_file"]
        == "abc.html"
    )


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
