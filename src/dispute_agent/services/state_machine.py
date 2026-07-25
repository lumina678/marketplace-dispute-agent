from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from dispute_agent.config import get_settings
from dispute_agent.errors import ConflictError, NotFoundError, StateTransitionError
from dispute_agent.ids import new_id
from dispute_agent.models import (
    Appeal,
    CaseEvent,
    CaseRun,
    Claim,
    Decision,
    Dispute,
    Evidence,
    ListingSnapshot,
    Message,
    OpenQuestion,
    ResolutionAction,
    User,
    utc_now,
)


class StateMachineService:
    def __init__(self, definition_path: Path | None = None):
        path = definition_path or get_settings().state_machine_path
        self.definition = json.loads(path.read_text(encoding="utf-8"))
        self.initial_state: str = self.definition["initial_state"]
        self.transitions = self.definition["transitions"]

    def transition(
        self,
        session: Session,
        *,
        case_id: str,
        trigger: str,
        actor_type: str,
        actor_id: str,
        reason_code: str,
        expected_state_version: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CaseEvent:
        dispute = session.get(Dispute, case_id)
        if dispute is None:
            raise NotFoundError(f"案件不存在: {case_id}")
        expected_version = expected_state_version or dispute.state_version
        if dispute.state_version != expected_version:
            raise ConflictError(
                f"状态版本冲突: expected={expected_version}, actual={dispute.state_version}"
            )
        candidates = [
            item
            for item in self.transitions
            if item["from"] == dispute.state and item["trigger"] == trigger
        ]
        if len(candidates) != 1:
            raise StateTransitionError(f"状态 {dispute.state} 不允许 trigger={trigger}")
        transition = candidates[0]
        if actor_type not in transition["actor_types"]:
            raise StateTransitionError(
                f"actor_type={actor_type} 不允许执行 {transition['id']}"
            )
        context = metadata or {}
        failed_guards = [
            guard
            for guard in transition["guards"]
            if not self._evaluate_guard(session, dispute, guard, actor_id, context)
        ]
        if failed_guards:
            raise StateTransitionError(
                f"转换 {transition['id']} 的前置条件不满足: {', '.join(failed_guards)}"
            )

        new_version = expected_version + 1
        result = session.execute(
            update(Dispute)
            .where(
                Dispute.id == case_id,
                Dispute.state == dispute.state,
                Dispute.state_version == expected_version,
            )
            .values(state=transition["to"], state_version=new_version, updated_at=utc_now())
        )
        if result.rowcount != 1:
            raise ConflictError("案件状态被并发修改，请重新读取后重试")
        sequence = (session.scalar(select(func.max(CaseEvent.sequence)).where(CaseEvent.dispute_id == case_id)) or 0) + 1
        event = CaseEvent(
            id=new_id("event"),
            dispute_id=case_id,
            sequence=sequence,
            event_type=trigger,
            transition_id=transition["id"],
            from_state=transition["from"],
            to_state=transition["to"],
            actor_type=actor_type,
            actor_id=actor_id,
            reason_code=reason_code,
            expected_state_version=expected_version,
            new_state_version=new_version,
            metadata_json=context,
        )
        session.add(event)
        session.flush()
        session.expire(dispute)
        return event

    def replay(self, session: Session, case_id: str) -> dict[str, Any]:
        dispute = session.get(Dispute, case_id)
        if dispute is None:
            raise NotFoundError(f"案件不存在: {case_id}")
        state = self.initial_state
        version = 1
        events = list(
            session.scalars(
                select(CaseEvent).where(CaseEvent.dispute_id == case_id).order_by(CaseEvent.sequence)
            )
        )
        for expected_sequence, event in enumerate(events, start=1):
            if event.sequence != expected_sequence:
                raise ConflictError(f"事件序号不连续: expected={expected_sequence}, actual={event.sequence}")
            if event.from_state != state or event.expected_state_version != version:
                raise ConflictError(f"事件 {event.id} 无法从重放状态 {state}@{version} 应用")
            if event.new_state_version != version + 1:
                raise ConflictError(f"事件 {event.id} 的版本增量不是 1")
            state = event.to_state
            version = event.new_state_version
        consistent = state == dispute.state and version == dispute.state_version
        if not consistent:
            raise ConflictError(
                f"重放结果 {state}@{version} 与数据库 {dispute.state}@{dispute.state_version} 不一致"
            )
        return {"case_id": case_id, "state": state, "state_version": version, "event_count": len(events), "consistent": True}

    def _evaluate_guard(
        self,
        session: Session,
        dispute: Dispute,
        guard: str,
        actor_id: str,
        metadata: dict[str, Any],
    ) -> bool:
        transaction = dispute.transaction
        open_questions = lambda target=None: list(
            session.scalars(
                select(OpenQuestion).where(
                    OpenQuestion.dispute_id == dispute.id,
                    OpenQuestion.status == "OPEN",
                    *([OpenQuestion.target == target] if target else []),
                )
            )
        )
        latest_decision = lambda: session.scalar(
            select(Decision).where(Decision.dispute_id == dispute.id).order_by(Decision.version.desc()).limit(1)
        )
        all_actions = lambda: list(
            session.scalars(select(ResolutionAction).where(ResolutionAction.dispute_id == dispute.id))
        )
        actions = lambda: list(
            session.scalars(
                select(ResolutionAction).where(
                    ResolutionAction.decision_id == (latest_decision().id if latest_decision() else "")
                )
            )
        )
        latest_appeal = lambda: session.scalar(
            select(Appeal).where(Appeal.dispute_id == dispute.id).order_by(Appeal.submitted_at.desc()).limit(1)
        )
        actor = session.get(User, actor_id)
        active_run = session.get(CaseRun, dispute.active_case_run_id) if dispute.active_case_run_id else None

        checks: dict[str, Any] = {
            "transaction_exists": transaction is not None,
            "claim_exists": session.scalar(select(func.count()).select_from(Claim).where(Claim.dispute_id == dispute.id)) > 0,
            "snapshots_hashed": (
                all(
                    len(item.content_sha256) == 64
                    for item in session.scalars(select(ListingSnapshot).where(ListingSnapshot.transaction_id == dispute.transaction_id))
                )
                and bool(session.scalar(select(func.count()).select_from(ListingSnapshot).where(ListingSnapshot.transaction_id == dispute.transaction_id)))
                and all(
                    item.snapshot_locked and len(item.content_sha256) == 64
                    for item in session.scalars(select(Message).where(Message.transaction_id == dispute.transaction_id))
                )
                and bool(session.scalar(select(func.count()).select_from(Message).where(Message.transaction_id == dispute.transaction_id)))
            ),
            "no_irreversible_action_executed": not any(item.status == "SUCCEEDED" for item in all_actions()),
            "policy_version_uniquely_selected": bool(dispute.policy_id and dispute.policy_version),
            "case_run_created": active_run is not None,
            "policy_missing_or_ambiguous": bool(metadata.get("policy_selection_failed")),
            "open_buyer_question_exists": bool(open_questions("BUYER")),
            "open_seller_question_exists": bool(open_questions("SELLER")),
            "question_round_within_limit": dispute.question_round_count <= 3,
            "response_linked_to_open_question": bool(metadata.get("response_linked_to_question_ids")),
            "question_deadline_reached": bool(metadata.get("question_deadline_reached")),
            "question_round_limit_reached": dispute.question_round_count >= 3,
            "decision_draft_exists": latest_decision() is not None,
            "no_blocking_open_question": not bool(open_questions()),
            "guard_has_no_block_violation": bool(metadata.get("guard_passed")),
            "budget_or_risk_limit_reached": bool(metadata.get("budget_or_risk_limit_reached")),
            "decision_version_frozen": bool(latest_decision() and latest_decision().content_sha256),
            "review_package_hash_exists": bool(metadata.get("review_package_hash")),
            "reviewer_authorized": bool(actor and actor.role == "REVIEWER"),
            "specific_decision_version_selected": bool(metadata.get("decision_id")),
            "reviewer_instructions_exist": bool(metadata.get("reviewer_instructions")),
            "revision_instructions_exist": bool(metadata.get("revision_instructions")),
            "final_outcome_recorded": bool(latest_decision() and latest_decision().status in {"APPROVED", "REJECTED"}),
            "no_execution_action_required": not bool(actions()),
            "approved_actions_exist": bool(actions()) and all(item.status == "APPROVED" for item in actions()),
            "idempotency_keys_exist": bool(actions()) and all(item.idempotency_key for item in actions()),
            "amounts_within_transaction_total": all(
                item.amount_minor is None or item.amount_minor <= transaction.paid_amount_minor for item in actions()
            ),
            "approved_actions_empty": not bool(actions()),
            "all_actions_verified_by_readback": bool(actions()) and all(item.status == "SUCCEEDED" for item in actions()),
            "action_failed_or_status_unknown": any(item.status in {"FAILED", "UNKNOWN"} for item in actions()),
            "idempotency_status_checked": bool(metadata.get("idempotency_status_checked")),
            "retry_is_safe": bool(metadata.get("retry_is_safe")),
            "appeal_within_deadline": bool(metadata.get("appeal_within_deadline")),
            "appeal_reason_exists": bool(latest_appeal() and latest_appeal().statement.strip()),
            "no_open_appeal": not bool(session.scalar(select(func.count()).select_from(Appeal).where(Appeal.dispute_id == dispute.id, Appeal.status.in_(["SUBMITTED", "UNDER_REVIEW"])))),
            "new_evidence_or_material_error_exists": bool(metadata.get("new_evidence_or_material_error_exists")),
            "appeal_review_completed": bool(latest_appeal() and latest_appeal().status in {"ACCEPTED", "DENIED"}),
            "new_case_run_created": bool(active_run),
            "prior_decision_preserved": bool(latest_decision()),
            "material_new_evidence_exists": bool(metadata.get("material_new_evidence_exists")),
            "admin_authorized": bool(actor and actor.role == "ADMIN"),
        }
        if guard not in checks:
            raise StateTransitionError(f"状态机 guard 尚未实现，安全拒绝: {guard}")
        return bool(checks[guard])
