import enum
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

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
    lifecycle_events: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )

    contents: Mapped[list["Content"]] = relationship(back_populates="snapshot")
    headers: Mapped[list["Header"]] = relationship(back_populates="snapshot")


class Content(Base):
    __tablename__ = "content"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snapshot.id"), nullable=False
    )
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    hash: Mapped[str] = mapped_column(Text, nullable=False)

    snapshot: Mapped["Snapshot"] = relationship(back_populates="contents")


class Header(Base):
    __tablename__ = "header"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("snapshot.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    snapshot: Mapped["Snapshot"] = relationship(back_populates="headers")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session
