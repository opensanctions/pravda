"""initial schema

Revision ID: f2c15ccc6215
Revises:
Create Date: 2026-07-06 17:40:13.589752

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c15ccc6215"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the initial schema."""
    # The condition_type enum is created and dropped explicitly so the
    # round-trip is clean. alembic's drop_table does not remove an enum that
    # create_table created inline, which would leave the type orphaned and
    # break a subsequent upgrade ("type ... already exists").
    conditiontype = postgresql.ENUM("lifecycle", "selector", name="conditiontype")
    conditiontype.create(op.get_bind(), checkfirst=False)

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
        sa.Column(
            "condition_type",
            postgresql.ENUM(
                "lifecycle", "selector", name="conditiontype", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("condition", sa.Text(), nullable=False),
        sa.Column("condition_met", sa.Boolean(), nullable=False),
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
    conditiontype = postgresql.ENUM("lifecycle", "selector", name="conditiontype")
    conditiontype.drop(op.get_bind(), checkfirst=False)
