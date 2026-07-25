"""add appeal appellant, policy and decision provenance fields

Revision ID: 9c2f3d6e4a11
Revises: fcbcee15aca3
Create Date: 2026-07-23 19:10:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9c2f3d6e4a11"
down_revision: Union[str, Sequence[str], None] = "fcbcee15aca3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Persist the provenance needed to enforce safe appeal handling.

    The initial schema intentionally kept appeals small.  Steps 13–14 need to
    pin the appellant identity, transaction-time policy version, original
    decision hash and the appeal deadline.  All fields are nullable during the
    migration so existing historical rows remain readable; new submissions are
    validated by ``AppealService``.
    """
    with op.batch_alter_table("appeals") as batch_op:
        batch_op.add_column(sa.Column("appellant_id", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("policy_id", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("policy_version", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("decision_content_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("deadline", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_appeals_appellant_id_users",
            "users",
            ["appellant_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "ck_appeals_decision_hash",
            "decision_content_sha256 IS NULL OR length(decision_content_sha256) = 64",
        )


def downgrade() -> None:
    with op.batch_alter_table("appeals") as batch_op:
        batch_op.drop_constraint("ck_appeals_decision_hash", type_="check")
        batch_op.drop_constraint("fk_appeals_appellant_id_users", type_="foreignkey")
        batch_op.drop_column("deadline")
        batch_op.drop_column("decision_content_sha256")
        batch_op.drop_column("policy_version")
        batch_op.drop_column("policy_id")
        batch_op.drop_column("appellant_id")
