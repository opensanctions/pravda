"""Durable web evidence capture for web pages.

Construct a :class:`Pravda` with explicit :class:`PravdaConfig` settings::

    from pravda import Pravda, PravdaConfig

    config = PravdaConfig(
        browser_ws_url=...,
        storage_base_path=...,
    )
    pravda = Pravda(config)
    snapshot = await pravda.capture(url)

Capture returns a :class:`Snapshot` value with client-minted ``id`` and
``captured_at``; artifact fields are storage-relative names, read back
with ``storage.read_artifact(name, snapshot.final_url)``. Persisting
snapshots — including failed captures, returned with ``error`` set — is
the application's policy: Pravda owns the browser and content-addressed
artifact storage.

Capture budgets bound the target website only and default to
``Timeouts()``; browser machinery liveness deadlines default to
``Liveness()``. Browser machinery failures raise
:class:`InfrastructureError` and capture nothing.
"""

from pravda.config import Liveness, PravdaConfig, Timeouts
from pravda.pravda import InfrastructureError, Pravda
from pravda.snapshots import Snapshot
from pravda.storage import Storage

__all__ = [
    "InfrastructureError",
    "Liveness",
    "Pravda",
    "PravdaConfig",
    "Snapshot",
    "Storage",
    "Timeouts",
]
