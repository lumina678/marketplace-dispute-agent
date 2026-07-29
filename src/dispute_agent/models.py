from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from dispute_agent.db import Base
from dispute_agent.ids import new_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AwareDateTime(TypeDecorator[datetime]):
    """Use ISO text in SQLite and native ``TIMESTAMPTZ`` in PostgreSQL."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime(timezone=True))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: datetime | None, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime values must include a timezone")
        normalized = value.astimezone(timezone.utc)
        return normalized if dialect.name == "postgresql" else normalized.isoformat()

    def process_result_value(self, value, _dialect) -> datetime | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    username: Mapped[str | None] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    simulated_balance_minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        CheckConstraint("role IN ('BUYER','SELLER','REVIEWER','ADMIN')", name="ck_users_role"),
        CheckConstraint("simulated_balance_minor >= 0", name="ck_users_balance_nonnegative"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    buyer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    seller_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    listing_id: Mapped[str] = mapped_column(String(80), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="USED_LAPTOP")
    paid_amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    paid_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    order_status: Mapped[str] = mapped_column(String(30), nullable=False, default="PAID")
    funds_status: Mapped[str] = mapped_column(String(30), nullable=False, default="HELD")
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    buyer: Mapped[User] = relationship(foreign_keys=[buyer_id])
    seller: Mapped[User] = relationship(foreign_keys=[seller_id])

    __table_args__ = (
        CheckConstraint("paid_amount_minor >= 0", name="ck_transactions_amount_nonnegative"),
        CheckConstraint("currency = 'CNY'", name="ck_transactions_currency_cny"),
        Index("ix_transactions_listing_id", "listing_id"),
    )


class ListingSnapshot(Base):
    __tablename__ = "listing_snapshots"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    listing_id: Mapped[str] = mapped_column(String(80), nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(30), nullable=False, default="DISPUTE_BASELINE")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    immutable_uri: Mapped[str] = mapped_column(String(255), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)

    transaction: Mapped[Transaction] = relationship()

    __table_args__ = (
        UniqueConstraint("transaction_id", "snapshot_type", name="uq_listing_snapshot_transaction_type"),
        CheckConstraint("length(content_sha256) = 64", name="ck_listing_snapshot_sha256"),
    )


class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    recorded_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    dispute_type: Mapped[str] = mapped_column(String(40), nullable=False, default="DESCRIPTION_MISMATCH")
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="SUBMITTED")
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active_case_run_id: Mapped[str | None] = mapped_column(String(80))
    question_round_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    human_review_reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    policy_id: Mapped[str | None] = mapped_column(String(120))
    policy_version: Mapped[str | None] = mapped_column(String(20))
    policy_basis_time: Mapped[datetime | None] = mapped_column(AwareDateTime())
    intake_manifest_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    intake_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    materials_frozen_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, onupdate=utc_now, nullable=False)

    transaction: Mapped[Transaction] = relationship()
    claims: Mapped[list[Claim]] = relationship(back_populates="dispute", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "dispute_type IN ('DESCRIPTION_MISMATCH','MISSING_PARTS','EMPTY_PACKAGE','SHIPPING_DAMAGE','COUNTERFEIT','OTHER')",
            name="ck_disputes_dispute_type",
        ),
        CheckConstraint("state_version >= 1", name="ck_disputes_state_version"),
        CheckConstraint("question_round_count BETWEEN 0 AND 3", name="ck_disputes_question_round"),
        Index("ix_disputes_transaction_id", "transaction_id"),
        Index("ix_disputes_state", "state"),
    )


class Claim(Base):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    party: Mapped[str] = mapped_column(String(10), nullable=False)
    claim_type: Mapped[str] = mapped_column(String(50), nullable=False)
    issue_type: Mapped[str] = mapped_column(String(40), nullable=False)
    issue_subtype: Mapped[str | None] = mapped_column(String(80))
    routing_source: Mapped[str] = mapped_column(String(30), nullable=False)
    routing_reason: Mapped[str | None] = mapped_column(Text)
    routing_confidence: Mapped[float | None] = mapped_column(Float)
    skill_name: Mapped[str | None] = mapped_column(String(80))
    skill_version: Mapped[str | None] = mapped_column(String(20))
    routing_status: Mapped[str] = mapped_column(String(20), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="ALLEGED")
    material: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    responds_to_claim_id: Mapped[str | None] = mapped_column(ForeignKey("claims.id"))
    asserted_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    dispute: Mapped[Dispute] = relationship(back_populates="claims", foreign_keys=[dispute_id])

    __table_args__ = (
        CheckConstraint("party IN ('BUYER','SELLER')", name="ck_claims_party"),
        CheckConstraint(
            "issue_type IN ('DESCRIPTION_MISMATCH','MISSING_PARTS','EMPTY_PACKAGE','SHIPPING_DAMAGE','COUNTERFEIT','OTHER')",
            name="ck_claims_issue_type",
        ),
        CheckConstraint(
            "routing_source IN ('USER_DECLARED','PLATFORM_REASON_CODE','DETERMINISTIC_RULE','MODEL_SUGGESTION','REVIEWER_OVERRIDE','LEGACY_MIGRATION')",
            name="ck_claims_routing_source",
        ),
        CheckConstraint(
            "routing_status IN ('UNROUTED','ROUTED','NEEDS_HUMAN','OVERRIDDEN')",
            name="ck_claims_routing_status",
        ),
        CheckConstraint(
            "routing_confidence IS NULL OR (routing_confidence >= 0 AND routing_confidence <= 1)",
            name="ck_claims_routing_confidence",
        ),
        CheckConstraint(
            "(skill_name IS NULL AND skill_version IS NULL) OR (skill_name IS NOT NULL AND skill_version IS NOT NULL)",
            name="ck_claims_skill_binding_pair",
        ),
        Index("ix_claims_dispute_party", "dispute_id", "party"),
        Index("ix_claims_dispute_issue", "dispute_id", "issue_type"),
        Index("ix_claims_skill_binding", "skill_name", "skill_version"),
    )


class ClaimRoutingDecision(Base):
    __tablename__ = "claim_routing_decisions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    claim_id: Mapped[str] = mapped_column(ForeignKey("claims.id", ondelete="CASCADE"), nullable=False)
    case_run_id: Mapped[str | None] = mapped_column(ForeignKey("case_runs.id", ondelete="SET NULL"))
    decision_version: Mapped[int] = mapped_column(Integer, nullable=False)
    router_id: Mapped[str] = mapped_column(String(120), nullable=False)
    router_version: Mapped[str] = mapped_column(String(20), nullable=False)
    issue_type: Mapped[str] = mapped_column(String(40), nullable=False)
    claim_type: Mapped[str] = mapped_column(String(50), nullable=False)
    routing_source: Mapped[str] = mapped_column(String(30), nullable=False)
    routing_status: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    matched_signals_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    skill_name: Mapped[str | None] = mapped_column(String(80))
    skill_version: Mapped[str | None] = mapped_column(String(20))
    requires_human_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("claim_id", "decision_version", name="uq_claim_routing_decisions_claim_version"),
        UniqueConstraint("claim_id", "input_fingerprint", name="uq_claim_routing_decisions_claim_input"),
        CheckConstraint("decision_version >= 1", name="ck_claim_routing_decisions_version"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_claim_routing_decisions_confidence"),
        CheckConstraint("length(input_fingerprint) = 64", name="ck_claim_routing_decisions_input_hash"),
        CheckConstraint("length(content_sha256) = 64", name="ck_claim_routing_decisions_content_hash"),
        CheckConstraint(
            "(skill_name IS NULL AND skill_version IS NULL) OR (skill_name IS NOT NULL AND skill_version IS NOT NULL)",
            name="ck_claim_routing_decisions_skill_pair",
        ),
        Index("ix_claim_routing_decisions_dispute_time", "dispute_id", "created_at"),
        Index("ix_claim_routing_decisions_claim_version", "claim_id", "decision_version"),
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    dispute_id: Mapped[str | None] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"))
    sender_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    sender_role: Mapped[str] = mapped_column(String(10), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("sender_role IN ('BUYER','SELLER')", name="ck_messages_sender_role"),
        CheckConstraint("length(content_sha256) = 64", name="ck_messages_sha256"),
        Index("ix_messages_transaction_sent", "transaction_id", "sent_at"),
    )


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(20), nullable=False)
    recorded_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    evidence_type: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text)
    source_system: Mapped[str] = mapped_column(String(40), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(120), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    immutable_uri: Mapped[str] = mapped_column(String(255), nullable=False)
    integrity_status: Mapped[str] = mapped_column(String(30), nullable=False)
    related_claim_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    extracted_facts_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    handling_flags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (
        CheckConstraint("length(content_sha256) = 64", name="ck_evidence_sha256"),
        UniqueConstraint("dispute_id", "content_sha256", name="uq_evidence_dispute_sha256"),
        Index("ix_evidence_dispute_type", "dispute_id", "evidence_type"),
    )


class ShipmentEvent(Base):
    __tablename__ = "shipment_events"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    location: Mapped[str | None] = mapped_column(String(120))
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (Index("ix_shipment_transaction_time", "transaction_id", "occurred_at"),)


class PolicyVersion(Base):
    __tablename__ = "policy_versions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: new_id("policy"))
    policy_id: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(AwareDateTime())
    document_path: Mapped[str] = mapped_column(String(255), nullable=False)
    document_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_policy_versions_id_version"),
        Index("ix_policy_versions_effective", "policy_id", "effective_from", "effective_to"),
    )


class CaseRun(Base):
    __tablename__ = "case_runs"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="CREATED")
    phase: Mapped[str] = mapped_column(String(40), nullable=False, default="INTAKE")
    checkpoint_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    tool_call_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    tool_calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=40000)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    phase_failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    paused_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("dispute_id", "run_number", name="uq_case_runs_dispute_number"),
        CheckConstraint("tool_calls_used >= 0 AND tool_calls_used <= tool_call_budget", name="ck_case_runs_tool_budget"),
        CheckConstraint("tokens_used >= 0 AND tokens_used <= token_budget", name="ck_case_runs_token_budget"),
        Index("ix_case_runs_dispute_status", "dispute_id", "status"),
    )


class WorkflowJob(Base):
    __tablename__ = "workflow_jobs"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    case_run_id: Mapped[str | None] = mapped_column(ForeignKey("case_runs.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="QUEUED")
    current_phase: Mapped[str | None] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(String(80), nullable=False)
    active_dedupe_key: Mapped[str | None] = mapped_column(String(80), unique=True)
    rq_job_id: Mapped[str | None] = mapped_column(String(160), unique=True)
    delivery_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=600)
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(160))
    heartbeat_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    attempt_deadline_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    pause_requested_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    cancel_requested_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    paused_reason: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','RETRYING','PAUSE_REQUESTED','PAUSED','CANCEL_REQUESTED','CANCELLED','COMPLETED','FAILED','TIMED_OUT')",
            name="ck_workflow_jobs_status",
        ),
        CheckConstraint("delivery_version >= 1", name="ck_workflow_jobs_delivery_version"),
        CheckConstraint("attempt_count >= 0 AND attempt_count <= max_attempts", name="ck_workflow_jobs_attempt_count"),
        CheckConstraint("max_attempts >= 1", name="ck_workflow_jobs_max_attempts"),
        CheckConstraint("timeout_seconds >= 30", name="ck_workflow_jobs_timeout"),
        CheckConstraint("event_sequence >= 0", name="ck_workflow_jobs_event_sequence"),
        Index("ix_workflow_jobs_case_created", "dispute_id", "created_at"),
        Index("ix_workflow_jobs_status_heartbeat", "status", "heartbeat_at"),
    )


class WorkflowStage(Base):
    __tablename__ = "workflow_stages"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    workflow_job_id: Mapped[str] = mapped_column(ForeignKey("workflow_jobs.id", ondelete="CASCADE"), nullable=False)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    case_run_id: Mapped[str | None] = mapped_column(ForeignKey("case_runs.id", ondelete="SET NULL"))
    phase: Mapped[str] = mapped_column(String(40), nullable=False)
    agent_role: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    result_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("workflow_job_id", "phase", "agent_role", name="uq_workflow_stages_job_phase_role"),
        CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','PAUSED','CANCELLED')",
            name="ck_workflow_stages_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_workflow_stages_attempt_count"),
        Index("ix_workflow_stages_job_status", "workflow_job_id", "status"),
    )


class WorkflowJobEvent(Base):
    __tablename__ = "workflow_job_events"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    workflow_job_id: Mapped[str] = mapped_column(ForeignKey("workflow_jobs.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(40))
    agent_role: Mapped[str | None] = mapped_column(String(60))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("workflow_job_id", "sequence", name="uq_workflow_job_events_sequence"),
        CheckConstraint("sequence >= 1", name="ck_workflow_job_events_sequence"),
        Index("ix_workflow_job_events_stream", "workflow_job_id", "sequence"),
    )


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str | None] = mapped_column(ForeignKey("disputes.id", ondelete="SET NULL"))
    case_run_id: Mapped[str | None] = mapped_column(ForeignKey("case_runs.id", ondelete="SET NULL"))
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(100))
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    called_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        CheckConstraint("duration_ms >= 0", name="ck_tool_calls_duration"),
        Index("ix_tool_calls_case_run", "case_run_id", "called_at"),
        Index("ix_tool_calls_name", "tool_name"),
    )


class AgentOutput(Base):
    __tablename__ = "agent_outputs"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    case_run_id: Mapped[str] = mapped_column(ForeignKey("case_runs.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False)
    output_type: Mapped[str] = mapped_column(String(40), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False, default="1.0.0")
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "case_run_id",
            "role",
            "output_type",
            "input_fingerprint",
            name="uq_agent_outputs_run_role_input",
        ),
        CheckConstraint("length(input_fingerprint) = 64", name="ck_agent_outputs_input_hash"),
        CheckConstraint("length(content_sha256) = 64", name="ck_agent_outputs_content_hash"),
        Index("ix_agent_outputs_case_role", "dispute_id", "role", "created_at"),
        Index("ix_agent_outputs_run_type", "case_run_id", "output_type"),
    )


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    case_run_id: Mapped[str | None] = mapped_column(ForeignKey("case_runs.id", ondelete="SET NULL"))
    agent_output_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_outputs.id", ondelete="SET NULL")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    supersedes_decision_id: Mapped[str | None] = mapped_column(ForeignKey("decisions.id"))
    outcome: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="DRAFT")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("dispute_id", "version", name="uq_decisions_dispute_version"),
        UniqueConstraint("agent_output_id", name="uq_decisions_agent_output_id"),
        CheckConstraint("version >= 1", name="ck_decisions_version"),
        CheckConstraint("length(content_sha256) = 64", name="ck_decisions_sha256"),
    )


class DecisionGuardReport(Base):
    __tablename__ = "decision_guard_reports"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    case_run_id: Mapped[str] = mapped_column(ForeignKey("case_runs.id", ondelete="CASCADE"), nullable=False)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id", ondelete="CASCADE"), nullable=False)
    guard_version: Mapped[str] = mapped_column(String(20), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    required_action: Mapped[str] = mapped_column(String(40), nullable=False)
    violations_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    checks_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    decision_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("decision_id", name="uq_guard_reports_decision"),
        CheckConstraint("length(decision_content_sha256) = 64", name="ck_guard_reports_decision_hash"),
        CheckConstraint("length(content_sha256) = 64", name="ck_guard_reports_content_hash"),
        Index("ix_guard_reports_case_run", "dispute_id", "case_run_id"),
    )


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id", ondelete="CASCADE"), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decision_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("decision_id", "action", name="uq_approvals_decision_action"),
        CheckConstraint("action IN ('APPROVE','REJECT')", name="ck_approvals_action"),
    )


class Appeal(Base):
    __tablename__ = "appeals"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), nullable=False)
    appellant_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    appellant_role: Mapped[str] = mapped_column(String(10), nullable=False)
    recorded_by_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    grounds: Mapped[str] = mapped_column(String(30), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    policy_id: Mapped[str | None] = mapped_column(String(120))
    policy_version: Mapped[str | None] = mapped_column(String(20))
    decision_content_sha256: Mapped[str | None] = mapped_column(String(64))
    deadline: Mapped[datetime | None] = mapped_column(AwareDateTime())
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="SUBMITTED")
    submitted_at: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(AwareDateTime())
    resolution_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "decision_content_sha256 IS NULL OR length(decision_content_sha256) = 64",
            name="ck_appeals_decision_hash",
        ),
        Index("ix_appeals_dispute_status", "dispute_id", "status"),
    )


class ResolutionAction(Base):
    __tablename__ = "resolution_actions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    decision_id: Mapped[str] = mapped_column(ForeignKey("decisions.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    amount_minor: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CNY")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="DRAFT")
    external_reference: Mapped[str | None] = mapped_column(String(120))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, onupdate=utc_now, nullable=False)

    __table_args__ = (
        CheckConstraint("amount_minor IS NULL OR amount_minor >= 0", name="ck_resolution_actions_amount"),
        CheckConstraint("currency = 'CNY'", name="ck_resolution_actions_currency"),
        Index("ix_resolution_actions_dispute_status", "dispute_id", "status"),
    )


class CaseEvent(Base):
    __tablename__ = "case_events"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    transition_id: Mapped[str | None] = mapped_column(String(10))
    from_state: Mapped[str] = mapped_column(String(40), nullable=False)
    to_state: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    expected_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    new_state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("dispute_id", "sequence", name="uq_case_events_dispute_sequence"),
        CheckConstraint("new_state_version = expected_state_version + 1", name="ck_case_events_version_increment"),
        Index("ix_case_events_dispute_time", "dispute_id", "occurred_at"),
    )


class OpenQuestion(Base):
    __tablename__ = "open_questions"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    case_run_id: Mapped[str] = mapped_column(ForeignKey("case_runs.id", ondelete="CASCADE"), nullable=False)
    target: Mapped[str] = mapped_column(String(10), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    missing_fact: Mapped[str] = mapped_column(Text, nullable=False)
    resolves_claim_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    acceptable_evidence_types_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    response_evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    dedupe_key: Mapped[str | None] = mapped_column(String(64))
    basis_evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    generation_reason: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(AwareDateTime(), default=utc_now, nullable=False)
    deadline: Mapped[datetime] = mapped_column(AwareDateTime(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(AwareDateTime())

    __table_args__ = (
        CheckConstraint("target IN ('BUYER','SELLER')", name="ck_open_questions_target"),
        CheckConstraint("round_number BETWEEN 1 AND 3", name="ck_open_questions_round"),
        Index("ix_open_questions_dispute_status", "dispute_id", "status"),
        Index("ix_open_questions_dedupe", "dispute_id", "dedupe_key", "status"),
    )
