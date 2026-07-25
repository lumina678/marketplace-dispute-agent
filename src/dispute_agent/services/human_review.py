from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.config import Settings, get_settings
from dispute_agent.db import SessionLocal
from dispute_agent.errors import ConflictError, NotFoundError, ValidationError
from dispute_agent.ids import new_id
from dispute_agent.models import (
    AgentOutput,
    Approval,
    CaseRun,
    Claim,
    Decision,
    DecisionGuardReport,
    Dispute,
    Evidence,
    ResolutionAction,
    User,
    utc_now,
)
from dispute_agent.serialization import content_hash, jsonable
from dispute_agent.services.state_machine import StateMachineService
from dispute_agent.services.claim_routing import claim_routing_payload


class HumanReviewService:
    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        settings: Settings | None = None,
        state_machine: StateMachineService | None = None,
    ):
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.state_machine = state_machine or StateMachineService(self.settings.state_machine_path)

    def get_review_package(self, case_id: str, *, decision_id: str | None = None) -> dict[str, Any]:
        with self.session_factory() as session:
            if decision_id is None:
                dispute, run, decision, guard = self._review_context(session, case_id, require_human_state=False)
            else:
                dispute = session.get(Dispute, case_id)
                decision = session.get(Decision, decision_id)
                if dispute is None or decision is None or decision.dispute_id != case_id:
                    raise NotFoundError("案件或指定决定不存在")
                run = session.get(CaseRun, decision.case_run_id) if decision.case_run_id else None
                guard = session.scalar(
                    select(DecisionGuardReport).where(DecisionGuardReport.decision_id == decision.id)
                )
                if run is None or guard is None:
                    raise NotFoundError("指定决定没有完整审核包")
            outputs = list(
                session.scalars(
                    select(AgentOutput)
                    .where(AgentOutput.case_run_id == decision.case_run_id)
                    .order_by(AgentOutput.created_at)
                )
            )
            claims = list(session.scalars(select(Claim).where(Claim.dispute_id == case_id).order_by(Claim.asserted_at)))
            evidence = list(
                session.scalars(select(Evidence).where(Evidence.dispute_id == case_id).order_by(Evidence.submitted_at))
            )
            actions = list(
                session.scalars(
                    select(ResolutionAction)
                    .where(ResolutionAction.decision_id == decision.id)
                    .order_by(ResolutionAction.created_at)
                )
            )
            approvals = list(
                session.scalars(select(Approval).where(Approval.decision_id == decision.id).order_by(Approval.created_at))
            )
            guard_payload = run.checkpoint_json.get("phase_results", {}).get("GUARD_CHECK") or self._guard_payload(guard)
            expected_hash = content_hash({"decision": decision.content_sha256, "guard": guard_payload})
            package_hash = run.checkpoint_json.get("review_package_hash")
            guard_record_matches = bool(
                guard_payload.get("guard_result_id") == guard.id
                and guard_payload.get("content_sha256") == guard.content_sha256
                and guard_payload.get("decision_content_sha256") == decision.content_sha256
            )
            return jsonable(
                {
                    "case_id": case_id,
                    "state": dispute.state,
                    "state_version": dispute.state_version,
                    "case_run_id": run.id,
                    "review_package_hash": package_hash,
                    "package_integrity_valid": package_hash == expected_hash and guard_record_matches,
                    "decision": {
                        "decision_id": decision.id,
                        "version": decision.version,
                        "status": decision.status,
                        "outcome": decision.outcome,
                        "payload": decision.payload_json,
                        "content_sha256": decision.content_sha256,
                        "requires_human_review": decision.requires_human_review,
                    },
                    "guard": guard_payload,
                    "agent_outputs": [
                        {
                            "output_id": item.id,
                            "role": item.role,
                            "output_type": item.output_type,
                            "payload": item.payload_json,
                            "content_sha256": item.content_sha256,
                            "prompt_version": item.prompt_version,
                            "model_name": item.model_name,
                        }
                        for item in outputs
                    ],
                    "claims": [
                        {
                            "claim_id": item.id,
                            "party": item.party,
                            "claim_type": item.claim_type,
                            **claim_routing_payload(item),
                            "statement": item.statement,
                            "material": item.material,
                        }
                        for item in claims
                    ],
                    "evidence": [
                        {
                            "evidence_id": item.id,
                            "submitted_by": item.submitted_by,
                            "evidence_type": item.evidence_type,
                            "description": item.description,
                            "integrity_status": item.integrity_status,
                            "content_sha256": item.content_sha256,
                        }
                        for item in evidence
                    ],
                    "resolution_actions": [self._action_view(item) for item in actions],
                    "review_records": [
                        {
                            "approval_id": item.id,
                            "reviewer_id": item.reviewer_id,
                            "action": item.action,
                            "reason": item.reason,
                            "decision_content_sha256": item.decision_content_sha256,
                            "created_at": item.created_at,
                        }
                        for item in approvals
                    ],
                }
            )

    def approve(
        self,
        case_id: str,
        *,
        decision_id: str,
        reviewer_id: str,
        reason: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("批准原因不能为空")
        with self.session_factory() as session:
            reviewer = self._require_reviewer(session, reviewer_id)
            dispute, run, decision, guard = self._review_context(session, case_id, require_human_state=False)
            if decision.id != decision_id:
                raise ConflictError("只能批准审核包中固定的决定版本")
            existing_records = list(session.scalars(select(Approval).where(Approval.decision_id == decision.id)))
            if existing_records:
                existing = existing_records[0]
                if existing.action == "APPROVE" and existing.reviewer_id == reviewer.id:
                    return self._review_result(dispute, decision, existing, session)
                raise ConflictError("该决定已经存在不可兼容的审核记录")
            if dispute.state != "HUMAN_REVIEW":
                raise ConflictError("案件当前不在人工审核状态")
            if not guard.passed or guard.decision_content_sha256 != decision.content_sha256:
                raise ConflictError("未通过 Guard 或决定哈希已变化，不能批准")
            if decision.status != "DRAFT":
                raise ConflictError(f"当前决定状态不可批准: {decision.status}")
            package = self.get_review_package(case_id)
            if package["package_integrity_valid"] is not True:
                raise ConflictError("审核包哈希校验失败")

            approval = Approval(
                id=new_id("approval"),
                dispute_id=case_id,
                decision_id=decision.id,
                reviewer_id=reviewer.id,
                action="APPROVE",
                reason=reason.strip(),
                decision_content_sha256=decision.content_sha256,
            )
            session.add(approval)
            decision.status = "APPROVED"
            actions = list(session.scalars(select(ResolutionAction).where(ResolutionAction.decision_id == decision.id)))
            for action in actions:
                if action.status != "DRAFT":
                    raise ConflictError("审核包中的处置动作状态已变化")
                action.status = "APPROVED"
            session.flush()
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="DECISION_APPROVED",
                actor_type="REVIEWER",
                actor_id=reviewer.id,
                reason_code="REVIEWER_APPROVED_FIXED_DECISION",
                expected_state_version=dispute.state_version,
                metadata={"decision_id": decision.id, "review_reason": reason.strip()},
            )
            session.refresh(dispute)
            session.commit()
            return self._review_result(dispute, decision, approval, session)

    def reject(
        self,
        case_id: str,
        *,
        decision_id: str,
        reviewer_id: str,
        reason: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("拒绝原因不能为空")
        with self.session_factory() as session:
            reviewer = self._require_reviewer(session, reviewer_id)
            dispute, _run, decision, _guard = self._review_context(session, case_id, require_human_state=False)
            if decision.id != decision_id:
                raise ConflictError("只能拒绝审核包中固定的决定版本")
            existing_records = list(session.scalars(select(Approval).where(Approval.decision_id == decision.id)))
            if existing_records:
                existing = existing_records[0]
                if existing.action == "REJECT" and existing.reviewer_id == reviewer.id:
                    return self._review_result(dispute, decision, existing, session)
                raise ConflictError("该决定已经存在不可兼容的审核记录")
            if dispute.state != "HUMAN_REVIEW":
                raise ConflictError("案件当前不在人工审核状态")

            record = Approval(
                id=new_id("review"),
                dispute_id=case_id,
                decision_id=decision.id,
                reviewer_id=reviewer.id,
                action="REJECT",
                reason=reason.strip(),
                decision_content_sha256=decision.content_sha256,
            )
            session.add(record)
            decision.status = "REJECTED"
            for action in session.scalars(select(ResolutionAction).where(ResolutionAction.decision_id == decision.id)):
                action.status = "CANCELLED"
            session.flush()
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="DRAFT_REJECTED",
                actor_type="REVIEWER",
                actor_id=reviewer.id,
                reason_code="REVIEWER_REJECTED_DRAFT",
                expected_state_version=dispute.state_version,
                metadata={"decision_id": decision.id, "review_reason": reason.strip()},
            )
            session.refresh(dispute)
            session.commit()
            return self._review_result(dispute, decision, record, session)

    def return_to_investigation(
        self,
        case_id: str,
        *,
        decision_id: str,
        reviewer_id: str,
        instructions: str,
        claim_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not instructions.strip():
            raise ValidationError("退回调查指令不能为空")
        with self.session_factory() as session:
            reviewer = self._require_reviewer(session, reviewer_id)
            dispute, previous_run, decision, _guard = self._review_context(session, case_id, require_human_state=True)
            if decision.id != decision_id:
                raise ConflictError("只能退回审核包中固定的决定版本")
            material_claim_ids = set(
                session.scalars(select(Claim.id).where(Claim.dispute_id == case_id, Claim.material.is_(True)))
            )
            dirty_claim_ids = set(claim_ids or material_claim_ids)
            if not dirty_claim_ids or not dirty_claim_ids.issubset(material_claim_ids):
                raise ValidationError("claim_ids 必须引用当前案件的关键主张")

            run_number = (
                session.scalar(select(func.max(CaseRun.run_number)).where(CaseRun.dispute_id == case_id)) or 0
            ) + 1
            checkpoint = {
                "schema_version": "1.0.0",
                "resumed_from_run_id": previous_run.id,
                "reviewer_instructions": instructions.strip(),
                "dirty_claim_ids": sorted(dirty_claim_ids),
                "completed_phases": ["INTAKE", "SNAPSHOT", "CLAIM_EXTRACTION"],
                "phase_results": {},
                "pending_phase": "PARTY_ANALYSIS",
                "pending_agent_roles": ["BUYER_CASE_ANALYST", "SELLER_CASE_ANALYST"],
                "pause_reason": "REVIEWER_RETURNED_TO_INVESTIGATION",
                "supersedes_decision_id": decision.id,
            }
            new_run = CaseRun(
                id=new_id("run"),
                dispute_id=case_id,
                run_number=run_number,
                status="PAUSED",
                phase="PARTY_ANALYSIS",
                checkpoint_json=checkpoint,
                tool_call_budget=self.settings.default_tool_call_budget,
                token_budget=self.settings.default_token_budget,
                started_at=utc_now(),
                paused_at=utc_now(),
            )
            session.add(new_run)
            session.flush()
            dispute.active_case_run_id = new_run.id
            decision.status = "REVISION_REQUESTED"
            for action in session.scalars(select(ResolutionAction).where(ResolutionAction.decision_id == decision.id)):
                action.status = "CANCELLED"
            session.flush()
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="MORE_EVIDENCE_REQUIRED",
                actor_type="REVIEWER",
                actor_id=reviewer.id,
                reason_code="REVIEWER_RETURNED_FOR_MORE_INVESTIGATION",
                expected_state_version=dispute.state_version,
                metadata={
                    "decision_id": decision.id,
                    "reviewer_instructions": instructions.strip(),
                    "dirty_claim_ids": sorted(dirty_claim_ids),
                    "new_case_run_id": new_run.id,
                },
            )
            session.refresh(dispute)
            session.commit()
            return jsonable(
                {
                    "case_id": case_id,
                    "state": dispute.state,
                    "state_version": dispute.state_version,
                    "case_run_id": new_run.id,
                    "run_number": new_run.run_number,
                    "phase": new_run.phase,
                    "run_status": new_run.status,
                    "checkpoint": new_run.checkpoint_json,
                    "returned_decision_id": decision.id,
                }
            )

    @staticmethod
    def _require_reviewer(session: Session, reviewer_id: str) -> User:
        reviewer = session.get(User, reviewer_id)
        if reviewer is None or reviewer.role != "REVIEWER":
            raise ValidationError("reviewer_id 必须属于授权审核员")
        return reviewer

    @staticmethod
    def _review_context(
        session: Session,
        case_id: str,
        *,
        require_human_state: bool,
    ) -> tuple[Dispute, CaseRun, Decision, DecisionGuardReport]:
        dispute = session.get(Dispute, case_id)
        if dispute is None:
            raise NotFoundError(f"案件不存在: {case_id}")
        if require_human_state and dispute.state != "HUMAN_REVIEW":
            raise ConflictError("案件当前不在人工审核状态")
        run = session.get(CaseRun, dispute.active_case_run_id) if dispute.active_case_run_id else None
        if run is None:
            raise NotFoundError("案件没有活动 case_run")
        decision_id = run.checkpoint_json.get("decision_id")
        decision = session.get(Decision, decision_id) if decision_id else None
        if decision is None or decision.dispute_id != case_id:
            raise NotFoundError("审核包没有固定决定")
        guard = session.scalar(
            select(DecisionGuardReport).where(DecisionGuardReport.decision_id == decision.id)
        )
        if guard is None:
            raise NotFoundError("审核包没有 Guard 结果")
        return dispute, run, decision, guard

    @staticmethod
    def _guard_payload(guard: DecisionGuardReport) -> dict[str, Any]:
        return jsonable(
            {
                "guard_result_id": guard.id,
                "case_id": guard.dispute_id,
                "case_run_id": guard.case_run_id,
                "decision_id": guard.decision_id,
                "guard_version": guard.guard_version,
                "passed": guard.passed,
                "violations": guard.violations_json,
                "checks": guard.checks_json,
                "required_action": guard.required_action,
                "decision_content_sha256": guard.decision_content_sha256,
                "content_sha256": guard.content_sha256,
                "checked_at": guard.created_at,
            }
        )

    @staticmethod
    def _action_view(action: ResolutionAction) -> dict[str, Any]:
        return jsonable(
            {
                "action_id": action.id,
                "action_type": action.action_type,
                "amount_minor": action.amount_minor,
                "currency": action.currency,
                "idempotency_key": action.idempotency_key,
                "status": action.status,
                "external_reference": action.external_reference,
            }
        )

    @classmethod
    def _review_result(
        cls,
        dispute: Dispute,
        decision: Decision,
        record: Approval,
        session: Session,
    ) -> dict[str, Any]:
        actions = list(session.scalars(select(ResolutionAction).where(ResolutionAction.decision_id == decision.id)))
        return jsonable(
            {
                "case_id": dispute.id,
                "state": dispute.state,
                "state_version": dispute.state_version,
                "decision_id": decision.id,
                "decision_version": decision.version,
                "decision_status": decision.status,
                "review_record": {
                    "approval_id": record.id,
                    "reviewer_id": record.reviewer_id,
                    "action": record.action,
                    "reason": record.reason,
                    "decision_content_sha256": record.decision_content_sha256,
                },
                "resolution_actions": [cls._action_view(item) for item in actions],
            }
        )
