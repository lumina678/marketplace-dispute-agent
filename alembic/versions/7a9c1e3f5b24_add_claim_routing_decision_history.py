"""add claim routing decision history

Revision ID: 7a9c1e3f5b24
Revises: 2e4c6a8b0d12
Create Date: 2026-07-26 10:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7a9c1e3f5b24"
down_revision: Union[str, Sequence[str], None] = "2e4c6a8b0d12"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "claim_routing_decisions",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("dispute_id", sa.String(length=80), nullable=False),
        sa.Column("claim_id", sa.String(length=80), nullable=False),
        sa.Column("case_run_id", sa.String(length=80), nullable=True),
        sa.Column("decision_version", sa.Integer(), nullable=False),
        sa.Column("router_id", sa.String(length=120), nullable=False),
        sa.Column("router_version", sa.String(length=20), nullable=False),
        sa.Column("issue_type", sa.String(length=40), nullable=False),
        sa.Column("claim_type", sa.String(length=50), nullable=False),
        sa.Column("routing_source", sa.String(length=30), nullable=False),
        sa.Column("routing_status", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("matched_signals_json", sa.JSON(), nullable=False),
        sa.Column("skill_name", sa.String(length=80), nullable=True),
        sa.Column("skill_version", sa.String(length=20), nullable=True),
        sa.Column("requires_human_confirmation", sa.Boolean(), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("actor_id", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("decision_version >= 1", name="ck_claim_routing_decisions_version"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_claim_routing_decisions_confidence"),
        sa.CheckConstraint("length(input_fingerprint) = 64", name="ck_claim_routing_decisions_input_hash"),
        sa.CheckConstraint("length(content_sha256) = 64", name="ck_claim_routing_decisions_content_hash"),
        sa.CheckConstraint(
            "(skill_name IS NULL AND skill_version IS NULL) OR (skill_name IS NOT NULL AND skill_version IS NOT NULL)",
            name="ck_claim_routing_decisions_skill_pair",
        ),
        sa.ForeignKeyConstraint(["case_run_id"], ["case_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dispute_id"], ["disputes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("claim_id", "decision_version", name="uq_claim_routing_decisions_claim_version"),
        sa.UniqueConstraint("claim_id", "input_fingerprint", name="uq_claim_routing_decisions_claim_input"),
    )
    op.create_index(
        "ix_claim_routing_decisions_dispute_time",
        "claim_routing_decisions",
        ["dispute_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_claim_routing_decisions_claim_version",
        "claim_routing_decisions",
        ["claim_id", "decision_version"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_claim_routing_decisions_claim_version", table_name="claim_routing_decisions")
    op.drop_index("ix_claim_routing_decisions_dispute_time", table_name="claim_routing_decisions")
    op.drop_table("claim_routing_decisions")
