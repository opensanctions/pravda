"""Tests for the public ``Snapshot`` value and its mapping from records."""

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
    sessionmaker,
    url: str,
    captured_at: datetime,
    *,
    final_url: str | None = None,
    http_status: int | None = 200,
    rendered_html: str | None = None,
    screenshot: str | None = None,
    standalone_html: str | None = None,
    http_archive: dict | None = None,
) -> uuid.UUID:
    """Insert and commit a snapshot through the test sessionmaker fixture."""
    record_id = uuid.uuid4()
    record = SnapshotRecord(
        id=record_id,
        url=url,
        captured_at=captured_at,
        http_status=http_status,
        final_url=final_url,
        rendered_html=rendered_html,
        screenshot=screenshot,
        standalone_html=standalone_html,
        http_archive=http_archive,
    )
    async with sessionmaker() as session:
        session.add(record)
        await session.commit()
    return record_id


@pytest.mark.asyncio
async def test_from_record_resolves_paths_without_mutating_persisted_record(
    sessionmaker, tmp_path
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
        sessionmaker,
        "https://example.com",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        final_url="https://example.com/page",
        rendered_html="bbb.html",
        screenshot="ccc.png",
        standalone_html="ddd.html",
        http_archive=http_archive,
    )

    storage = Storage.from_url(str(tmp_path))
    async with sessionmaker() as session:
        record = (
            await session.execute(
                select(SnapshotRecord).where(
                    SnapshotRecord.url == "https://example.com"
                )
            )
        ).scalar_one()
        snapshot = from_record(record, storage)

    prefix = os.path.join(str(tmp_path), "example.com")
    assert snapshot.rendered_html == os.path.join(prefix, "bbb.html")
    assert snapshot.screenshot == os.path.join(prefix, "ccc.png")
    assert snapshot.standalone_html == os.path.join(prefix, "ddd.html")
    entries = snapshot.http_archive["log"]["entries"]
    assert entries[0]["response"]["content"]["_file"] == os.path.join(
        prefix, "abc.html"
    )
    assert "_file" not in entries[1]["response"]["content"]

    assert record.rendered_html == "bbb.html"
    assert record.screenshot == "ccc.png"
    assert record.standalone_html == "ddd.html"
    assert (
        record.http_archive["log"]["entries"][0]["response"]["content"]["_file"]
        == "abc.html"
    )


@pytest.mark.asyncio
async def test_snapshot_is_immutable(sessionmaker, tmp_path):
    url = "https://example.com"
    await _commit_snapshot(sessionmaker, url, datetime(2026, 1, 1, tzinfo=timezone.utc))

    storage = Storage.from_url(str(tmp_path))
    async with sessionmaker() as session:
        record = (
            await session.execute(
                select(SnapshotRecord).where(SnapshotRecord.url == url)
            )
        ).scalar_one()
        snapshot = from_record(record, storage)

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.url = "https://mutated.example"


def test_pravda_exports_public_names():
    assert pravda_pkg.Pravda is Pravda
    assert pravda_pkg.Snapshot is Snapshot
    assert callable(pravda_pkg.PravdaConfig)
