import enum
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
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
    __table_args__ = (
        CheckConstraint(
            "(blob IS NULL AND blob_content_type IS NULL) "
            "OR (blob IS NOT NULL AND blob_content_type IS NOT NULL)",
            name="blob_content_type_present_iff_blob",
        ),
    )

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

    # Captured evidence. Three are fixed-MIME (their type is implicit); the
    # blob is polymorphic (multipart/related today, application/pdf and others
    # later), so its content type is recorded alongside its hash.
    plaintext: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    screenshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    blob: Mapped[str | None] = mapped_column(Text, nullable=True)
    blob_content_type: Mapped[str | None] = mapped_column(Text, nullable=True)

    headers: Mapped[list["Header"]] = relationship(back_populates="snapshot")


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
