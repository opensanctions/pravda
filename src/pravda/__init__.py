"""Durable evidence capture for web pages.

Construct a :class:`Pravda` with explicit :class:`PravdaConfig` settings::

    from pravda import Pravda, PravdaConfig

    config = PravdaConfig(
        database_url=...,
        browser_ws_url=...,
        storage_base_path=...,
    )
    async with Pravda(config) as pravda:
        snapshot = await pravda.snapshot(url)
        history = await pravda.snapshots(url)

Applications own the Postgres database; bring it to the packaged schema head
from startup with the migration helper::

    import pravda
    await pravda.migrate(database_url)
"""

from pravda.migrate import migrate
from pravda.pravda import Pravda, PravdaConfig
from pravda.snapshots import Snapshot

__all__ = [
    "Pravda",
    "PravdaConfig",
    "Snapshot",
    "migrate",
]
