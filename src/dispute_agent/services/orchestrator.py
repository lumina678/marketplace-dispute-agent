from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.config import Settings, get_settings
from dispute_agent.db import SessionLocal
from dispute_agent.errors import BudgetExceededError, ConflictError, NotFoundError, ValidationError
from dispute_agent.ids import new_id
from dispute_agent.models import CaseRun, Decision, Dispute, Evidence, OpenQuestion, utc_now
from dispute_agent.serialization import content_hash, jsonable
from dispute_agent.services.policy import PolicyService
from dispute_agent.services.state_machine import StateMachineService
from dispute_agent.services.claim_routing import claim_routing_payload


PHASES = (
    "INTAKE",
    "SNAPSHOT",
    "CLAIM_EXTRACTION",
    "PARTY_ANALYSIS",
    "EVIDENCE_REVIEW",
    "GAP_RESOLUTION",
    "ADJUDICATION",
    "GUARD_CHECK",
    "HUMAN_REVIEW",
)


class CaseOrchestrator:
    """Deterministic harness. Agent implementations submit data; they never mutate state."""

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

    def start(self, case_id: str, *, actor_id: str = "orchestrator") -> dict[str, Any]:
        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            if dispute.active_case_run_id:
                active = session.get(CaseRun, dispute.active_case_run_id)
                if active and active.status in {"RUNNING", "PAUSED"}:
                    return self._run_view(dispute, active, resumed=True)
                if active and dispute.state in {
                    "HUMAN_REVIEW",
                    "APPROVED",
                    "REJECTED",
                    "EXECUTING",
                    "EXECUTION_FAILED",
                    "RESOLVED",
                    "CLOSED",
                }:
                    return self._run_view(dispute, active, resumed=True)
            if dispute.state not in {"SUBMITTED", "EVIDENCE_LOCKED"}:
                raise ConflictError(f"案件当前状态不能启动新调查: {dispute.state}")

            if dispute.state == "SUBMITTED":
                policy = PolicyService(session).select_for_transaction(
                    policy_id="marketplace.description_mismatch",
                    paid_at=dispute.transaction.paid_at,
                    dispute_type=dispute.dispute_type,
                    category=dispute.transaction.category,
                )
                dispute.policy_id = policy.policy_id
                dispute.policy_version = policy.version
                dispute.policy_basis_time = dispute.transaction.paid_at
                session.flush()
                self.state_machine.transition(
                    session,
                    case_id=case_id,
                    trigger="BASELINE_CAPTURED",
                    actor_type="ORCHESTRATOR",
                    actor_id=actor_id,
                    reason_code="MVP_BASELINE_VALIDATED",
                    expected_state_version=dispute.state_version,
                    metadata={"policy_id": policy.policy_id, "policy_version": policy.version},
                )
                session.refresh(dispute)

            run_number = (session.scalar(select(func.max(CaseRun.run_number)).where(CaseRun.dispute_id == case_id)) or 0) + 1
            run = CaseRun(
                id=new_id("run"),
                dispute_id=case_id,
                run_number=run_number,
                status="RUNNING",
                phase="INTAKE",
                checkpoint_json={
                    "schema_version": "1.0.0",
                    "completed_phases": [],
                    "phase_results": {},
                    "pending_phase": "INTAKE",
                    "pause_reason": None,
                },
                tool_call_budget=self.settings.default_tool_call_budget,
                token_budget=self.settings.default_token_budget,
                started_at=utc_now(),
            )
            session.add(run)
            session.flush()
            dispute.active_case_run_id = run.id
            session.flush()
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="INVESTIGATION_STARTED",
                actor_type="ORCHESTRATOR",
                actor_id=actor_id,
                reason_code="CASE_RUN_CREATED",
                expected_state_version=dispute.state_version,
                metadata={"case_run_id": run.id},
            )
            session.refresh(dispute)

            checkpoint = deepcopy(run.checkpoint_json)
            for phase in ("INTAKE", "SNAPSHOT", "CLAIM_EXTRACTION"):
                checkpoint["completed_phases"].append(phase)
                checkpoint["phase_results"][phase] = self._deterministic_phase_result(session, dispute, phase)
            checkpoint["pending_phase"] = "PARTY_ANALYSIS"
            checkpoint["pending_agent_roles"] = ["BUYER_CASE_ANALYST", "SELLER_CASE_ANALYST"]
            checkpoint["pause_reason"] = "AWAITING_PHASE_RESULT"
            run.phase = "PARTY_ANALYSIS"
            run.status = "PAUSED"
            run.paused_at = utc_now()
            run.checkpoint_json = checkpoint
            session.commit()
            return self._run_view(dispute, run)

    def submit_phase_result(
        self,
        case_id: str,
        *,
        case_run_id: str,
        phase: str,
        payload: dict[str, Any],
        actor: str,
        tokens_used: int = 0,
    ) -> dict[str, Any]:
        phase = phase.upper()
        if phase not in PHASES:
            raise ValidationError(f"未知编排阶段: {phase}")
        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            run = session.get(CaseRun, case_run_id)
            if dispute is None or run is None or run.dispute_id != case_id:
                raise NotFoundError("案件或 case_run 不存在")
            if dispute.active_case_run_id != run.id:
                raise ConflictError("只能向当前活动 case_run 提交结果")
            if dispute.state != "UNDER_INVESTIGATION":
                raise ConflictError(f"案件当前不在调查状态: {dispute.state}")
            if run.phase != phase:
                raise ConflictError(f"阶段不匹配: expected={run.phase}, actual={phase}")
            if run.status not in {"RUNNING", "PAUSED"}:
                raise ConflictError(f"case_run 当前不可提交结果: {run.status}")
            if tokens_used < 0:
                raise ValidationError("tokens_used 不能为负数")
            if run.tokens_used + tokens_used > run.token_budget or run.tool_calls_used > run.tool_call_budget:
                result = self._escalate_budget(session, dispute, run, actor)
                session.commit()
                return result

            run.tokens_used += tokens_used
            run.status = "RUNNING"
            run.paused_at = None
            checkpoint = deepcopy(run.checkpoint_json)
            phase_results = checkpoint.setdefault("phase_results", {})
            phase_results[phase] = payload
            completed = checkpoint.setdefault("completed_phases", [])
            if phase not in completed:
                completed.append(phase)
            checkpoint["last_submission"] = {"phase": phase, "actor": actor, "tokens_used": tokens_used, "at": utc_now().isoformat()}

            if phase == "PARTY_ANALYSIS":
                if not {"buyer_analysis", "seller_analysis"}.issubset(payload):
                    raise ValidationError("PARTY_ANALYSIS 必须同时包含 buyer_analysis 和 seller_analysis")
                next_phase = "EVIDENCE_REVIEW"
                checkpoint["pending_agent_roles"] = ["EVIDENCE_POLICY_CLERK"]
            elif phase == "EVIDENCE_REVIEW":
                next_phase = "GAP_RESOLUTION"
                checkpoint["pending_agent_roles"] = ["EVIDENCE_POLICY_CLERK"]
            elif phase == "GAP_RESOLUTION":
                question_ids = payload.get("question_ids", [])
                if question_ids:
                    questions = list(
                        session.scalars(
                            select(OpenQuestion).where(
                                OpenQuestion.dispute_id == case_id,
                                OpenQuestion.case_run_id == run.id,
                                OpenQuestion.id.in_(question_ids),
                                OpenQuestion.status == "OPEN",
                            )
                        )
                    )
                    if len(questions) != len(set(question_ids)):
                        raise ValidationError("GAP_RESOLUTION 引用了不存在或非开放的问题")
                    targets = {item.target for item in questions}
                    if len(targets) != 1:
                        raise ValidationError("一次 GAP_RESOLUTION 只能暂停等待同一方；双方问题应分轮处理")
                    target = targets.pop()
                    trigger = "BUYER_EVIDENCE_REQUIRED" if target == "BUYER" else "SELLER_EVIDENCE_REQUIRED"
                    self.state_machine.transition(
                        session,
                        case_id=case_id,
                        trigger=trigger,
                        actor_type="ORCHESTRATOR",
                        actor_id=actor,
                        reason_code="BLOCKING_EVIDENCE_GAP",
                        expected_state_version=dispute.state_version,
                        metadata={"question_ids": question_ids, "target": target},
                    )
                    session.refresh(dispute)
                    checkpoint["pending_phase"] = "GAP_RESOLUTION"
                    checkpoint["pause_reason"] = "WAITING_FOR_EXTERNAL_EVIDENCE"
                    checkpoint["waiting_for"] = target
                    checkpoint["question_ids"] = question_ids
                    run.status = "PAUSED"
                    run.paused_at = utc_now()
                    run.checkpoint_json = checkpoint
                    session.commit()
                    return self._run_view(dispute, run)
                next_phase = "ADJUDICATION"
                checkpoint["pending_agent_roles"] = ["ADJUDICATION_AGENT"]
            elif phase == "ADJUDICATION":
                decision_id = payload.get("decision_id")
                decision = session.get(Decision, decision_id) if decision_id else None
                if decision is None or decision.dispute_id != case_id or decision.case_run_id != run.id:
                    raise ValidationError("ADJUDICATION 必须引用当前 run 通过 resolution.create_draft 创建的决定")
                next_phase = "GUARD_CHECK"
                checkpoint["decision_id"] = decision.id
                checkpoint["pending_agent_roles"] = []
            elif phase == "GUARD_CHECK":
                if payload.get("passed") is not True:
                    run.phase_failure_count += 1
                    if run.phase_failure_count >= self.settings.max_phase_failures:
                        result = self._escalate_budget(session, dispute, run, actor, reason="GUARD_RETRY_LIMIT_REACHED")
                        session.commit()
                        return result
                    next_phase = "ADJUDICATION"
                    checkpoint["pause_reason"] = "GUARD_RETURNED_TO_ADJUDICATION"
                    checkpoint["pending_agent_roles"] = ["ADJUDICATION_AGENT"]
                else:
                    decision = session.get(Decision, checkpoint.get("decision_id"))
                    if decision is None:
                        raise ValidationError("GUARD_CHECK 找不到待检查决定")
                    review_hash = content_hash({"decision": decision.content_sha256, "guard": payload})
                    self.state_machine.transition(
                        session,
                        case_id=case_id,
                        trigger="INVESTIGATION_COMPLETED",
                        actor_type="ORCHESTRATOR",
                        actor_id=actor,
                        reason_code="GUARD_PASSED",
                        expected_state_version=dispute.state_version,
                        metadata={"guard_passed": True, "review_package_hash": review_hash},
                    )
                    session.refresh(dispute)
                    self.state_machine.transition(
                        session,
                        case_id=case_id,
                        trigger="REVIEW_PACKAGE_SUBMITTED",
                        actor_type="ORCHESTRATOR",
                        actor_id=actor,
                        reason_code="HUMAN_REVIEW_REQUIRED",
                        expected_state_version=dispute.state_version,
                        metadata={"review_package_hash": review_hash, "decision_id": decision.id},
                    )
                    session.refresh(dispute)
                    checkpoint["pending_phase"] = "HUMAN_REVIEW"
                    checkpoint["pause_reason"] = "AWAITING_HUMAN_REVIEW"
                    checkpoint["review_package_hash"] = review_hash
                    checkpoint["guard_result_id"] = payload.get("guard_result_id")
                    checkpoint["guard_content_sha256"] = payload.get("content_sha256")
                    run.phase = "HUMAN_REVIEW"
                    run.status = "COMPLETED"
                    run.completed_at = utc_now()
                    run.checkpoint_json = checkpoint
                    session.commit()
                    return self._run_view(dispute, run)
            else:
                raise ConflictError(f"阶段 {phase} 不能通过 Agent 结果推进")

            checkpoint["pending_phase"] = next_phase
            checkpoint["pause_reason"] = "AWAITING_PHASE_RESULT"
            run.phase = next_phase
            run.status = "PAUSED"
            run.paused_at = utc_now()
            run.checkpoint_json = checkpoint
            session.commit()
            return self._run_view(dispute, run)

    def resume_with_evidence(
        self,
        case_id: str,
        *,
        target: str,
        evidence_id: str,
        question_ids: list[str],
        actor_id: str = "orchestrator",
    ) -> dict[str, Any]:
        target = target.upper()
        expected_state = "WAITING_FOR_BUYER" if target == "BUYER" else "WAITING_FOR_SELLER"
        trigger = "BUYER_RESPONSE_RECEIVED" if target == "BUYER" else "SELLER_RESPONSE_RECEIVED"
        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            evidence = session.get(Evidence, evidence_id)
            if dispute is None or evidence is None or evidence.dispute_id != case_id:
                raise NotFoundError("案件或新证据不存在")
            if dispute.state != expected_state:
                raise ConflictError(f"案件不在 {target} 补证等待状态")
            questions = list(
                session.scalars(
                    select(OpenQuestion).where(
                        OpenQuestion.dispute_id == case_id,
                        OpenQuestion.target == target,
                        OpenQuestion.id.in_(question_ids),
                        OpenQuestion.status == "OPEN",
                    )
                )
            )
            if not question_ids or len(questions) != len(set(question_ids)):
                raise ValidationError("必须关联该方的有效开放问题")
            all_open_question_ids = set(
                session.scalars(
                    select(OpenQuestion.id).where(
                        OpenQuestion.dispute_id == case_id,
                        OpenQuestion.target == target,
                        OpenQuestion.status == "OPEN",
                    )
                )
            )
            if set(question_ids) != all_open_question_ids:
                raise ValidationError("一次补证响应必须覆盖当前等待方的全部开放问题")
            dirty_claim_ids: set[str] = set()
            for question in questions:
                question.status = "ANSWERED"
                question.response_evidence_ids_json = sorted(set(question.response_evidence_ids_json + [evidence_id]))
                question.resolved_at = utc_now()
                dirty_claim_ids.update(question.resolves_claim_ids_json)

            previous_run = session.get(CaseRun, dispute.active_case_run_id) if dispute.active_case_run_id else None
            if previous_run:
                previous_run.status = "COMPLETED"
                previous_run.completed_at = utc_now()
            run_number = (session.scalar(select(func.max(CaseRun.run_number)).where(CaseRun.dispute_id == case_id)) or 0) + 1
            checkpoint = {
                "schema_version": "1.0.0",
                "resumed_from_run_id": previous_run.id if previous_run else None,
                "new_evidence_ids": [evidence_id],
                "dirty_claim_ids": sorted(dirty_claim_ids),
                "completed_phases": ["INTAKE", "SNAPSHOT", "CLAIM_EXTRACTION"],
                "phase_results": {},
                "pending_phase": "PARTY_ANALYSIS",
                "pending_agent_roles": ["BUYER_CASE_ANALYST", "SELLER_CASE_ANALYST"],
                "pause_reason": "AWAITING_PHASE_RESULT",
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
            session.flush()
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger=trigger,
                actor_type="ORCHESTRATOR",
                actor_id=actor_id,
                reason_code="NEW_EVIDENCE_RECEIVED",
                expected_state_version=dispute.state_version,
                metadata={"response_linked_to_question_ids": question_ids, "evidence_id": evidence_id, "new_case_run_id": new_run.id},
            )
            session.refresh(dispute)
            session.commit()
            return self._run_view(dispute, new_run)

    def recover(self, case_id: str) -> dict[str, Any]:
        """Reload the active checkpoint after process restart without rerunning phases."""
        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            run = session.get(CaseRun, dispute.active_case_run_id) if dispute.active_case_run_id else None
            if run is None:
                raise NotFoundError("案件没有活动 case_run")
            if run.status == "RUNNING":
                checkpoint = deepcopy(run.checkpoint_json)
                checkpoint["pause_reason"] = "RECOVERED_AFTER_PROCESS_RESTART"
                run.status = "PAUSED"
                run.paused_at = utc_now()
                run.checkpoint_json = checkpoint
                session.commit()
            return self._run_view(dispute, run, resumed=True)

    def replay(self, case_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            return self.state_machine.replay(session, case_id)

    def _deterministic_phase_result(self, session: Session, dispute: Dispute, phase: str) -> dict[str, Any]:
        if phase == "INTAKE":
            return {
                "transaction_id": dispute.transaction_id,
                "dispute_type": dispute.dispute_type,
                "transaction_exists": True,
            }
        if phase == "SNAPSHOT":
            return {
                "policy_id": dispute.policy_id,
                "policy_version": dispute.policy_version,
                "policy_basis_time": dispute.policy_basis_time.isoformat() if dispute.policy_basis_time else None,
            }
        if phase == "CLAIM_EXTRACTION":
            return {
                "claim_ids": [claim.id for claim in dispute.claims],
                "material_claim_count": sum(1 for claim in dispute.claims if claim.material),
                "claim_routes": [
                    {
                        "claim_id": claim.id,
                        "claim_type": claim.claim_type,
                        **claim_routing_payload(claim),
                    }
                    for claim in dispute.claims
                ],
            }
        raise ValidationError(f"没有确定性阶段处理器: {phase}")

    def _escalate_budget(
        self,
        session: Session,
        dispute: Dispute,
        run: CaseRun,
        actor: str,
        *,
        reason: str = "BUDGET_EXCEEDED",
    ) -> dict[str, Any]:
        self.state_machine.transition(
            session,
            case_id=dispute.id,
            trigger="AUTOMATION_LIMIT_REACHED",
            actor_type="ORCHESTRATOR",
            actor_id=actor,
            reason_code=reason,
            expected_state_version=dispute.state_version,
            metadata={"budget_or_risk_limit_reached": True},
        )
        session.refresh(dispute)
        checkpoint = deepcopy(run.checkpoint_json)
        checkpoint["pause_reason"] = reason
        checkpoint["pending_phase"] = "HUMAN_REVIEW"
        run.phase = "HUMAN_REVIEW"
        run.status = "ESCALATED"
        run.completed_at = utc_now()
        run.checkpoint_json = checkpoint
        return self._run_view(dispute, run)

    @staticmethod
    def _run_view(dispute: Dispute, run: CaseRun, resumed: bool = False) -> dict[str, Any]:
        return jsonable(
            {
                "case_id": dispute.id,
                "state": dispute.state,
                "state_version": dispute.state_version,
                "case_run_id": run.id,
                "run_number": run.run_number,
                "run_status": run.status,
                "phase": run.phase,
                "checkpoint": run.checkpoint_json,
                "tool_calls": {"used": run.tool_calls_used, "budget": run.tool_call_budget},
                "tokens": {"used": run.tokens_used, "budget": run.token_budget},
                "resumed": resumed,
            }
        )
