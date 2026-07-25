"""add claim-level dispute routing and skill binding

Revision ID: 2e4c6a8b0d12
Revises: 9c2f3d6e4a11
Create Date: 2026-07-25 12:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2e4c6a8b0d12"
down_revision: Union[str, Sequence[str], None] = "9c2f3d6e4a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("disputes") as batch_op:
        batch_op.create_check_constraint(
            "ck_disputes_dispute_type",
            "dispute_type IN ('DESCRIPTION_MISMATCH','MISSING_PARTS','EMPTY_PACKAGE','SHIPPING_DAMAGE','COUNTERFEIT','OTHER')",
        )

    with op.batch_alter_table("claims") as batch_op:
        batch_op.add_column(sa.Column("issue_type", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("issue_subtype", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("routing_source", sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column("routing_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("routing_confidence", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("skill_name", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("skill_version", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("routing_status", sa.String(length=20), nullable=True))

    op.execute(
        sa.text(
            """
            UPDATE claims
            SET issue_type = 'DESCRIPTION_MISMATCH',
                issue_subtype = claim_type,
                routing_source = 'LEGACY_MIGRATION',
                routing_reason = 'Existing description-mismatch claim migrated to the initial versioned skill binding.',
                routing_confidence = 1.0,
                skill_name = 'description-mismatch',
                skill_version = '1.0.0',
                routing_status = 'ROUTED'
            """
        )
    )

    with op.batch_alter_table("claims") as batch_op:
        batch_op.alter_column("issue_type", existing_type=sa.String(length=40), nullable=False)
        batch_op.alter_column("routing_source", existing_type=sa.String(length=30), nullable=False)
        batch_op.alter_column("routing_status", existing_type=sa.String(length=20), nullable=False)
        batch_op.create_check_constraint(
            "ck_claims_issue_type",
            "issue_type IN ('DESCRIPTION_MISMATCH','MISSING_PARTS','EMPTY_PACKAGE','SHIPPING_DAMAGE','COUNTERFEIT','OTHER')",
        )
        batch_op.create_check_constraint(
            "ck_claims_routing_source",
            "routing_source IN ('USER_DECLARED','PLATFORM_REASON_CODE','DETERMINISTIC_RULE','MODEL_SUGGESTION','REVIEWER_OVERRIDE','LEGACY_MIGRATION')",
        )
        batch_op.create_check_constraint(
            "ck_claims_routing_status",
            "routing_status IN ('UNROUTED','ROUTED','NEEDS_HUMAN','OVERRIDDEN')",
        )
        batch_op.create_check_constraint(
            "ck_claims_routing_confidence",
            "routing_confidence IS NULL OR (routing_confidence >= 0 AND routing_confidence <= 1)",
        )
        batch_op.create_check_constraint(
            "ck_claims_skill_binding_pair",
            "(skill_name IS NULL AND skill_version IS NULL) OR (skill_name IS NOT NULL AND skill_version IS NOT NULL)",
        )
        batch_op.create_index("ix_claims_dispute_issue", ["dispute_id", "issue_type"], unique=False)
        batch_op.create_index("ix_claims_skill_binding", ["skill_name", "skill_version"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("claims") as batch_op:
        batch_op.drop_index("ix_claims_skill_binding")
        batch_op.drop_index("ix_claims_dispute_issue")
        batch_op.drop_constraint("ck_claims_skill_binding_pair", type_="check")
        batch_op.drop_constraint("ck_claims_routing_confidence", type_="check")
        batch_op.drop_constraint("ck_claims_routing_status", type_="check")
        batch_op.drop_constraint("ck_claims_routing_source", type_="check")
        batch_op.drop_constraint("ck_claims_issue_type", type_="check")
        batch_op.drop_column("routing_status")
        batch_op.drop_column("skill_version")
        batch_op.drop_column("skill_name")
        batch_op.drop_column("routing_confidence")
        batch_op.drop_column("routing_reason")
        batch_op.drop_column("routing_source")
        batch_op.drop_column("issue_subtype")
        batch_op.drop_column("issue_type")

    with op.batch_alter_table("disputes") as batch_op:
        batch_op.drop_constraint("ck_disputes_dispute_type", type_="check")

