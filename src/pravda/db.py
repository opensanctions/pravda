import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SnapshotRecord(Base):
    """A persisted snapshot row."""

    __tablename__ = "snapshot"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    plaintext: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    screenshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_archive: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True
    )
