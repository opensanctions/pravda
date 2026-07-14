"""initial schema

Revision ID: 06eb381b8ec5
Revises:
Create Date: 2026-07-13 19:32:50.054949

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "06eb381b8ec5"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the initial schema."""
    op.create_table(
        "snapshot",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("plaintext", sa.Text(), nullable=True),
        sa.Column("rendered_html", sa.Text(), nullable=True),
        sa.Column("screenshot", sa.Text(), nullable=True),
        sa.Column(
            "http_archive", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Drop the initial schema."""
    op.drop_table("snapshot")
