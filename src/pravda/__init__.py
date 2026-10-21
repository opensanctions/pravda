"""Durable web evidence capture for web pages.

Build a content-addressed :class:`Storage` once, then a long-lived
:class:`Pravda` over it::

    from pravda import Pravda, Storage

    storage = Storage.from_url(...)
    pravda = Pravda(browser_ws_url, storage)
    snapshot = await pravda.capture(url)

Capture returns a :class:`Snapshot` value with client-minted ``id`` and
``captured_at``; artifact fields are storage-relative names, read back
with ``storage.read_artifact(name, snapshot.final_url)``. Persisting
snapshots — including failed captures, returned with ``error`` set — is
the application's policy: Pravda owns the browser, and the storage
handle is the application's, shared by capture and later reads.

Capture budgets bound the target website only and default to
``Timeouts()``; browser machinery liveness deadlines default to
``Liveness()``. Browser machinery failures raise
:class:`InfrastructureError` and capture nothing.

Standalone HTML — a self-contained page rendering — is derived from a
snapshot's stored evidence::

    html = await snapshot.derive_standalone_html(storage)
"""

from pravda.config import Liveness, Timeouts
from pravda.pravda import InfrastructureError, Pravda
from pravda.snapshots import Snapshot
from pravda.storage import Storage

__all__ = [
    "InfrastructureError",
    "Liveness",
    "Pravda",
    "Snapshot",
    "Storage",
    "Timeouts",
]
