"""add generic intake evidence text and freeze manifest

Revision ID: b4d6e8f0a213
Revises: 7a9c1e3f5b24
Create Date: 2026-07-28 10:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b4d6e8f0a213"
down_revision: Union[str, Sequence[str], None] = "7a9c1e3f5b24"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("disputes", sa.Column("intake_manifest_json", sa.JSON(), nullable=True))
    op.add_column("disputes", sa.Column("intake_manifest_sha256", sa.String(length=64), nullable=True))
    op.add_column("disputes", sa.Column("materials_frozen_at", sa.Text(), nullable=True))
    op.add_column("evidence", sa.Column("content_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("evidence", "content_text")
    op.drop_column("disputes", "materials_frozen_at")
    op.drop_column("disputes", "intake_manifest_sha256")
    op.drop_column("disputes", "intake_manifest_json")
