import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

engine = create_async_engine(os.environ["DATABASE_URL"])
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class SnapshotRecord(Base):
    """A persisted snapshot row (the ``snapshot`` table).

    This is the SQLAlchemy ORM mapping — a storage detail. The public,
    immutable domain value is ``pravda.snapshots.Snapshot``; ``from_record``
    converts a row into one.
    """

    __tablename__ = "snapshot"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Captured evidence. Each is a content-addressed filename
    # (``<sha1>.<extension>``) under the shared storage backend; the
    # extension carries the artifact's type, so no separate MIME column.
    plaintext: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    screenshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The recorded HAR manifest, stored inline as JSON. Each entry's
    # ``response.content._file`` names a body stored as a content-addressed
    # blob (``<sha1>.<extension>``) under the storage prefix.
    http_archive: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
