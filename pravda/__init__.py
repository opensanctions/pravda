"""Durable evidence capture for web pages."""

from pravda.session import snapshot
from pravda.snapshots import Snapshot, snapshots

__all__ = [
    "Snapshot",
    "snapshot",
    "snapshots",
]
