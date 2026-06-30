import enum
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

engine = create_async_engine(os.environ["DATABASE_URL"])
async_session = async_sessionmaker(engine, expire_on_commit=False)


class ConditionType(enum.Enum):
    lifecycle = "lifecycle"
    selector = "selector"


class Base(DeclarativeBase):
    pass


class Snapshot(Base):
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
    condition_type: Mapped[ConditionType] = mapped_column(
        Enum(ConditionType), nullable=False
    )
    condition: Mapped[str] = mapped_column(Text, nullable=False)
    condition_met: Mapped[bool] = mapped_column(Boolean, nullable=False)

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


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
