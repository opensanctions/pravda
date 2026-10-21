"""Tests for the public ``Snapshot`` value."""

import dataclasses
import uuid
from datetime import datetime, timezone

import pytest

import pravda as pravda_pkg
from pravda import Pravda, Snapshot, Storage


def _snapshot() -> Snapshot:
    """A snapshot value with relative artifact names."""
    return Snapshot(
        id=uuid.uuid4(),
        url="https://example.com",
        final_url="https://example.com/page",
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        http_status=200,
        error=None,
        rendered_html="bbb.html",
        screenshot="ccc.png",
        http_archive=None,
    )


def test_snapshot_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _snapshot().url = "https://mutated.example"


def test_pravda_exports_public_names():
    assert pravda_pkg.Pravda is Pravda
    assert pravda_pkg.Snapshot is Snapshot
    assert pravda_pkg.Storage is Storage
    assert callable(pravda_pkg.PravdaConfig)
