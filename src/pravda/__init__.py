"""Durable evidence capture for web pages.

Construct a :class:`Pravda` with explicit :class:`PravdaConfig` settings and
an application-owned async session factory::

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from pravda import Pravda, PravdaConfig

    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    config = PravdaConfig(
        browser_ws_url=...,
        storage_base_path=...,
    )

    pravda = Pravda(config, sessionmaker)
    snapshot = await pravda.snapshot(url)
    history = await pravda.snapshots(url)

Applications own the database engine and dispose it on shutdown. Bring the
database to the packaged schema head from startup with the migration helper::

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
