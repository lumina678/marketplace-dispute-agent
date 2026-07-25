from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.db import SessionLocal
from dispute_agent.errors import NotFoundError, ValidationError
from dispute_agent.models import (
    AgentOutput,
    Appeal,
    Approval,
    CaseEvent,
    CaseRun,
    Decision,
    DecisionGuardReport,
    Dispute,
    Evidence,
    Message,
    OpenQuestion,
    ResolutionAction,
    ToolCall,
    User,
    utc_now,
)
from dispute_agent.seed import seed_database
from dispute_agent.serialization import jsonable


TEXT_DEMO_CASE_ID = "case_clear_mismatch"


class DemoCaseService:
    """Reset the text-only showcase without rebuilding the whole database."""

    def __init__(self, session_factory: sessionmaker[Session] = SessionLocal):
        self.session_factory = session_factory

    def reset_text_demo(self, case_id: str) -> dict[str, object]:
        if case_id != TEXT_DEMO_CASE_ID:
            raise ValidationError("目前只允许重置主演示案件 case_clear_mismatch")

        with self.session_factory() as session:
            # Refresh idempotent text chat and document fixtures first.
            seed_database(session)
            dispute = session.get(Dispute, case_id)
            if dispute is None:
                raise NotFoundError(f"演示案件不存在: {case_id}")

            decisions = list(session.scalars(select(Decision).where(Decision.dispute_id == case_id)))
            for decision in decisions:
                decision.supersedes_decision_id = None
            session.flush()

            for model in (
                ResolutionAction,
                Appeal,
                Approval,
                DecisionGuardReport,
                Decision,
                AgentOutput,
                OpenQuestion,
                ToolCall,
                CaseEvent,
                CaseRun,
            ):
                session.execute(delete(model).where(model.dispute_id == case_id))

            # Keep immutable seeded evidence and remove evidence created by prior
            # interactive supplement rounds.
            session.execute(
                delete(Evidence).where(
                    Evidence.dispute_id == case_id,
                    ~Evidence.id.like("ev_clear_mismatch_%"),
                )
            )

            dispute.state = "SUBMITTED"
            dispute.state_version = 1
            dispute.active_case_run_id = None
            dispute.question_round_count = 0
            dispute.requires_human_review = True
            dispute.human_review_reasons_json = []
            dispute.policy_id = None
            dispute.policy_version = None
            dispute.policy_basis_time = None
            dispute.updated_at = utc_now()
            dispute.transaction.order_status = "DISPUTED"
            dispute.transaction.funds_status = "HELD"

            for user_id in (dispute.transaction.buyer_id, dispute.transaction.seller_id):
                user = session.get(User, user_id)
                if user is not None:
                    user.simulated_balance_minor = 1_000_000

            session.commit()

        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            assert dispute is not None
            message_count = len(
                list(session.scalars(select(Message).where(Message.dispute_id == case_id)))
            )
            evidence_count = len(
                list(session.scalars(select(Evidence).where(Evidence.dispute_id == case_id)))
            )
            return jsonable(
                {
                    "case_id": case_id,
                    "state": dispute.state,
                    "state_version": dispute.state_version,
                    "text_only": True,
                    "message_count": message_count,
                    "evidence_count": evidence_count,
                    "ready": message_count >= 3 and evidence_count >= 2,
                }
            )
