"""use native PostgreSQL timestamptz columns

Revision ID: e3b7a1c9d5f2
Revises: c8a1f7d2e9b4
Create Date: 2026-07-29 10:00:00
"""

from typing import Sequence, Union

from alembic import op


revision: str = "e3b7a1c9d5f2"
down_revision: Union[str, Sequence[str], None] = "c8a1f7d2e9b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TIMESTAMP_COLUMNS: dict[str, tuple[str, ...]] = {
    "policy_versions": ("effective_from", "effective_to", "loaded_at"),
    "users": ("created_at",),
    "transactions": ("paid_at", "delivered_at", "created_at"),
    "disputes": ("policy_basis_time", "materials_frozen_at", "created_at", "updated_at"),
    "listing_snapshots": ("captured_at",),
    "shipment_events": ("occurred_at",),
    "case_events": ("occurred_at",),
    "case_runs": ("started_at", "paused_at", "completed_at", "created_at"),
    "claims": ("asserted_at", "created_at"),
    "evidence": ("captured_at", "submitted_at"),
    "messages": ("sent_at",),
    "agent_outputs": ("created_at",),
    "claim_routing_decisions": ("created_at",),
    "open_questions": ("created_at", "deadline", "resolved_at"),
    "tool_calls": ("called_at",),
    "workflow_jobs": (
        "heartbeat_at",
        "attempt_deadline_at",
        "pause_requested_at",
        "cancel_requested_at",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    ),
    "decisions": ("created_at",),
    "workflow_job_events": ("occurred_at",),
    "workflow_stages": ("heartbeat_at", "started_at", "completed_at", "created_at", "updated_at"),
    "appeals": ("deadline", "submitted_at", "resolved_at"),
    "approvals": ("created_at",),
    "decision_guard_reports": ("created_at",),
    "resolution_actions": ("created_at", "updated_at"),
}


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name, column_names in TIMESTAMP_COLUMNS.items():
        for column_name in column_names:
            op.execute(
                f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" '
                f'TYPE TIMESTAMP WITH TIME ZONE USING "{column_name}"::timestamptz'
            )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table_name, column_names in reversed(tuple(TIMESTAMP_COLUMNS.items())):
        for column_name in reversed(column_names):
            op.execute(
                f'ALTER TABLE "{table_name}" ALTER COLUMN "{column_name}" '
                f'TYPE TEXT USING "{column_name}"::text'
            )
