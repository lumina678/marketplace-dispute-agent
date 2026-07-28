"""add durable asynchronous workflow jobs

Revision ID: c8a1f7d2e9b4
Revises: b4d6e8f0a213
Create Date: 2026-07-28 18:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8a1f7d2e9b4"
down_revision: Union[str, Sequence[str], None] = "b4d6e8f0a213"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_jobs",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("dispute_id", sa.String(length=80), nullable=False),
        sa.Column("case_run_id", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("current_phase", sa.String(length=40), nullable=True),
        sa.Column("actor_id", sa.String(length=80), nullable=False),
        sa.Column("active_dedupe_key", sa.String(length=80), nullable=True),
        sa.Column("rq_job_id", sa.String(length=160), nullable=True),
        sa.Column("delivery_version", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("event_sequence", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=160), nullable=True),
        sa.Column("heartbeat_at", sa.Text(), nullable=True),
        sa.Column("attempt_deadline_at", sa.Text(), nullable=True),
        sa.Column("pause_requested_at", sa.Text(), nullable=True),
        sa.Column("cancel_requested_at", sa.Text(), nullable=True),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('QUEUED','RUNNING','RETRYING','PAUSE_REQUESTED','PAUSED','CANCEL_REQUESTED','CANCELLED','COMPLETED','FAILED','TIMED_OUT')",
            name="ck_workflow_jobs_status",
        ),
        sa.CheckConstraint("delivery_version >= 1", name="ck_workflow_jobs_delivery_version"),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_workflow_jobs_attempt_count",
        ),
        sa.CheckConstraint("max_attempts >= 1", name="ck_workflow_jobs_max_attempts"),
        sa.CheckConstraint("timeout_seconds >= 30", name="ck_workflow_jobs_timeout"),
        sa.CheckConstraint("event_sequence >= 0", name="ck_workflow_jobs_event_sequence"),
        sa.ForeignKeyConstraint(["case_run_id"], ["case_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dispute_id"], ["disputes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_dedupe_key"),
        sa.UniqueConstraint("rq_job_id"),
    )
    op.create_index("ix_workflow_jobs_case_created", "workflow_jobs", ["dispute_id", "created_at"])
    op.create_index("ix_workflow_jobs_status_heartbeat", "workflow_jobs", ["status", "heartbeat_at"])

    op.create_table(
        "workflow_stages",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("workflow_job_id", sa.String(length=80), nullable=False),
        sa.Column("dispute_id", sa.String(length=80), nullable=False),
        sa.Column("case_run_id", sa.String(length=80), nullable=True),
        sa.Column("phase", sa.String(length=40), nullable=False),
        sa.Column("agent_role", sa.String(length=60), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("heartbeat_at", sa.Text(), nullable=True),
        sa.Column("result_summary_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','PAUSED','CANCELLED')",
            name="ck_workflow_stages_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_workflow_stages_attempt_count"),
        sa.ForeignKeyConstraint(["case_run_id"], ["case_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dispute_id"], ["disputes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workflow_job_id"], ["workflow_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_job_id",
            "phase",
            "agent_role",
            name="uq_workflow_stages_job_phase_role",
        ),
    )
    op.create_index("ix_workflow_stages_job_status", "workflow_stages", ["workflow_job_id", "status"])

    op.create_table(
        "workflow_job_events",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("workflow_job_id", sa.String(length=80), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=60), nullable=False),
        sa.Column("phase", sa.String(length=40), nullable=True),
        sa.Column("agent_role", sa.String(length=60), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.CheckConstraint("sequence >= 1", name="ck_workflow_job_events_sequence"),
        sa.ForeignKeyConstraint(["workflow_job_id"], ["workflow_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workflow_job_id", "sequence", name="uq_workflow_job_events_sequence"),
    )
    op.create_index(
        "ix_workflow_job_events_stream",
        "workflow_job_events",
        ["workflow_job_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_job_events_stream", table_name="workflow_job_events")
    op.drop_table("workflow_job_events")
    op.drop_index("ix_workflow_stages_job_status", table_name="workflow_stages")
    op.drop_table("workflow_stages")
    op.drop_index("ix_workflow_jobs_status_heartbeat", table_name="workflow_jobs")
    op.drop_index("ix_workflow_jobs_case_created", table_name="workflow_jobs")
    op.drop_table("workflow_jobs")
