"""Durable web evidence capture for web pages.

Build a content-addressed :class:`Storage` once, then a long-lived
:class:`Pravda` over it::

    from pravda import Pravda, Storage

    storage = Storage.from_url(...)
    pravda = Pravda(browser_ws_url, storage)
    snapshot = await pravda.capture(url)

Capture returns a :class:`Snapshot` value with client-minted ``id`` and
``captured_at``; artifact fields are storage-relative paths, read back
with ``storage.read_artifact(path)``. Persisting
snapshots — including failed captures, returned with ``error`` set — is
the application's policy: Pravda owns the browser, and the storage
handle is the application's, shared by capture and later reads.

Capture budgets default to ``Timeouts()`` and bound one capture
attempt, closing the recording context included. A browser setup
failure raises :class:`InfrastructureError` and captures nothing; every
other capture failure is returned with ``error`` set.

Standalone HTML — a self-contained page rendering — is derived from a
snapshot's stored evidence and stored as a content-addressed artifact
(the ``unmhtml`` extra must be installed)::

    path = await snapshot.derive_standalone_html(storage)
"""

from pravda.config import Timeouts
from pravda.pravda import InfrastructureError, Pravda
from pravda.snapshots import Snapshot
from pravda.storage import Storage

__all__ = [
    "InfrastructureError",
    "Pravda",
    "Snapshot",
    "Storage",
    "Timeouts",
]
