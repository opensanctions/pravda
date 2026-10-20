"""Durable evidence capture for web pages.

Construct a :class:`Pravda` with explicit :class:`PravdaConfig` settings and
an application-owned async session factory::

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from pravda import Pravda, PravdaConfig

    engine = create_async_engine(database_url)
    sessionmaker = async_sessionmaker(engine)
    config = PravdaConfig(
        browser_ws_url=...,
        storage_base_path=...,
    )

    pravda = Pravda(config, sessionmaker)
    snapshot = await pravda.snapshot(url)

Applications own the database engine and dispose it on shutdown.

Capture budgets bound the target website only and default to ``Timeouts()``;
browser machinery liveness deadlines default to ``Liveness()``. Browser
machinery failures raise :class:`InfrastructureError` and persist nothing.
"""

from pravda.config import Liveness, PravdaConfig, Timeouts
from pravda.pravda import InfrastructureError, Pravda
from pravda.snapshots import Snapshot

__all__ = [
    "InfrastructureError",
    "Liveness",
    "Pravda",
    "PravdaConfig",
    "Snapshot",
    "Timeouts",
]
