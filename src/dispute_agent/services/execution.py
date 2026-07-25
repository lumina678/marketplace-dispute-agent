from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.config import Settings, get_settings
from dispute_agent.db import SessionLocal
from dispute_agent.errors import ConflictError, NotFoundError
from dispute_agent.models import Approval, CaseRun, Decision, Dispute, ResolutionAction, Transaction, User
from dispute_agent.serialization import jsonable
from dispute_agent.services.state_machine import StateMachineService
from dispute_agent.services.tools import ToolService


ACTION_ORDER = {
    "CREATE_RETURN": 10,
    "FREEZE_FUNDS": 20,
    "FULL_REFUND": 30,
    "PARTIAL_REFUND": 30,
    "RELEASE_FUNDS": 30,
}


class ResolutionExecutionService:
    """Execute only a reviewer-approved plan against the local simulated ledger."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        settings: Settings | None = None,
        state_machine: StateMachineService | None = None,
        tools: ToolService | None = None,
    ):
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.state_machine = state_machine or StateMachineService(self.settings.state_machine_path)
        self.tools = tools or ToolService(session_factory)

    def execute(
        self,
        case_id: str,
        *,
        decision_id: str,
        actor_id: str = "mock-executor",
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute, decision, actions = self._execution_context(session, case_id, decision_id)
            if dispute.state == "RESOLVED":
                return self._status_view(session, dispute, decision, idempotent_replay=True)
            if dispute.state == "EXECUTION_FAILED":
                raise ConflictError("执行处于失败状态，必须先完成幂等状态检查并调用 retry")
            if dispute.state == "APPROVED":
                if not actions:
                    self.state_machine.transition(
                        session,
                        case_id=case_id,
                        trigger="NO_EXECUTION_REQUIRED",
                        actor_type="SYSTEM",
                        actor_id=actor_id,
                        reason_code="APPROVED_DECISION_HAS_NO_ACTIONS",
                        expected_state_version=dispute.state_version,
                        metadata={"decision_id": decision.id},
                    )
                    session.refresh(dispute)
                    session.commit()
                    return self._status_view(session, dispute, decision, idempotent_replay=False)
                self.state_machine.transition(
                    session,
                    case_id=case_id,
                    trigger="EXECUTION_STARTED",
                    actor_type="EXECUTOR",
                    actor_id=actor_id,
                    reason_code="APPROVED_PLAN_EXECUTION_STARTED",
                    expected_state_version=dispute.state_version,
                    metadata={"decision_id": decision.id, "action_ids": [item.id for item in actions]},
                )
                session.refresh(dispute)
                session.commit()
            elif dispute.state != "EXECUTING":
                raise ConflictError(f"案件当前不可执行: {dispute.state}")

        action_results: list[dict[str, Any]] = []
        for action in sorted(actions, key=lambda item: (ACTION_ORDER.get(item.action_type, 999), item.created_at)):
            try:
                result = self.tools.call(
                    "resolution.execute_mock",
                    {
                        "case_id": case_id,
                        "action_id": action.id,
                        "idempotency_key": action.idempotency_key,
                    },
                    actor="EXECUTOR",
                )
                action_results.append(result)
            except Exception as exc:
                return self._record_failure(
                    case_id,
                    decision_id=decision_id,
                    action_id=action.id,
                    actor_id=actor_id,
                    error=exc,
                    prior_results=action_results,
                )

        with self.session_factory() as session:
            dispute, decision, actions = self._execution_context(session, case_id, decision_id)
            readback = self._verify_readback(session, dispute, decision, actions)
            if not readback["verified"]:
                candidate = next((item for item in actions if item.status != "SUCCEEDED"), actions[-1])
                candidate.status = "UNKNOWN"
                candidate.failure_code = "READBACK_MISMATCH"
                candidate.result_json = {**(candidate.result_json or {}), "readback": readback}
                session.flush()
                self.state_machine.transition(
                    session,
                    case_id=case_id,
                    trigger="EXECUTION_FAILED_OR_UNKNOWN",
                    actor_type="EXECUTOR",
                    actor_id=actor_id,
                    reason_code="EXECUTION_READBACK_MISMATCH",
                    expected_state_version=dispute.state_version,
                    metadata={"decision_id": decision.id, "readback": readback},
                )
            else:
                self.state_machine.transition(
                    session,
                    case_id=case_id,
                    trigger="EXECUTION_VERIFIED",
                    actor_type="EXECUTOR",
                    actor_id=actor_id,
                    reason_code="ALL_MOCK_ACTIONS_VERIFIED_BY_READBACK",
                    expected_state_version=dispute.state_version,
                    metadata={
                        "decision_id": decision.id,
                        "action_ids": [item.id for item in actions],
                        "readback": readback,
                    },
                )
            session.refresh(dispute)
            session.commit()
            result = self._status_view(session, dispute, decision, idempotent_replay=False)
            result["action_results"] = action_results
            result["readback"] = readback
            return result

    def retry(
        self,
        case_id: str,
        *,
        decision_id: str,
        actor_id: str = "mock-executor",
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute, decision, actions = self._execution_context(session, case_id, decision_id)
            if dispute.state != "EXECUTION_FAILED":
                if dispute.state == "RESOLVED":
                    return self._status_view(session, dispute, decision, idempotent_replay=True)
                raise ConflictError("案件当前不在 EXECUTION_FAILED 状态")
            status_checked = all(item.status in {"SUCCEEDED", "FAILED", "UNKNOWN"} for item in actions)
            retry_safe = all(
                item.status == "SUCCEEDED"
                or (item.status in {"FAILED", "UNKNOWN"} and item.external_reference is None)
                for item in actions
            )
            if not status_checked or not retry_safe:
                raise ConflictError("幂等状态检查无法证明重试安全")
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="IDEMPOTENT_RETRY_AUTHORIZED",
                actor_type="EXECUTOR",
                actor_id=actor_id,
                reason_code="LOCAL_IDEMPOTENCY_STATUS_CHECKED",
                expected_state_version=dispute.state_version,
                metadata={
                    "decision_id": decision.id,
                    "idempotency_status_checked": True,
                    "retry_is_safe": True,
                    "action_statuses": {item.id: item.status for item in actions},
                },
            )
            session.refresh(dispute)
            session.commit()
        return self.execute(case_id, decision_id=decision_id, actor_id=actor_id)

    def get_status(self, case_id: str, *, decision_id: str | None = None) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            decision = session.get(Decision, decision_id) if decision_id else self._latest_decision(session, case_id)
            if decision is None or decision.dispute_id != case_id:
                raise NotFoundError("决定不存在")
            return self._status_view(session, dispute, decision, idempotent_replay=False)

    def _record_failure(
        self,
        case_id: str,
        *,
        decision_id: str,
        action_id: str,
        actor_id: str,
        error: Exception,
        prior_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute, decision, _actions = self._execution_context(session, case_id, decision_id)
            action = session.get(ResolutionAction, action_id)
            if action is None:
                raise NotFoundError("执行失败动作不存在")
            if action.status != "SUCCEEDED":
                action.status = "FAILED"
                action.failure_code = error.__class__.__name__.upper()
                action.result_json = {"error": str(error), "safe_to_retry": action.external_reference is None}
            session.flush()
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="EXECUTION_FAILED_OR_UNKNOWN",
                actor_type="EXECUTOR",
                actor_id=actor_id,
                reason_code="MOCK_ACTION_EXECUTION_FAILED",
                expected_state_version=dispute.state_version,
                metadata={
                    "decision_id": decision.id,
                    "failed_action_id": action.id,
                    "error_type": error.__class__.__name__,
                },
            )
            session.refresh(dispute)
            session.commit()
            result = self._status_view(session, dispute, decision, idempotent_replay=False)
            result["action_results"] = prior_results
            result["error"] = {"type": error.__class__.__name__, "message": str(error)}
            return result

    def _execution_context(
        self,
        session: Session,
        case_id: str,
        decision_id: str,
    ) -> tuple[Dispute, Decision, list[ResolutionAction]]:
        dispute = session.get(Dispute, case_id)
        decision = session.get(Decision, decision_id)
        if dispute is None or decision is None or decision.dispute_id != case_id:
            raise NotFoundError("案件或决定不存在")
        latest = self._latest_decision(session, case_id)
        if latest is None or latest.id != decision.id:
            raise ConflictError("只能执行当前最新且已批准的决定版本")
        approval = session.scalar(
            select(Approval).where(Approval.decision_id == decision.id, Approval.action == "APPROVE")
        )
        if approval is None or approval.decision_content_sha256 != decision.content_sha256:
            raise ConflictError("决定缺少哈希一致的人工批准记录")
        reviewer = session.get(User, approval.reviewer_id)
        if reviewer is None or reviewer.role not in {"REVIEWER", "ADMIN"}:
            raise ConflictError("决定批准记录不是由审核员或管理员创建")
        if decision.status != "APPROVED":
            raise ConflictError(f"决定当前不可执行: {decision.status}")
        actions = list(
            session.scalars(
                select(ResolutionAction)
                .where(ResolutionAction.decision_id == decision.id)
                .order_by(ResolutionAction.created_at)
            )
        )
        return dispute, decision, actions

    @staticmethod
    def _latest_decision(session: Session, case_id: str) -> Decision | None:
        return session.scalar(
            select(Decision).where(Decision.dispute_id == case_id).order_by(Decision.version.desc()).limit(1)
        )

    @staticmethod
    def _verify_readback(
        session: Session,
        dispute: Dispute,
        decision: Decision,
        actions: list[ResolutionAction],
    ) -> dict[str, Any]:
        transaction = session.get(Transaction, dispute.transaction_id)
        assert transaction is not None
        action_types = {item.action_type for item in actions}
        checks = {
            "all_actions_succeeded": bool(actions) and all(
                item.status == "SUCCEEDED" and item.external_reference and item.result_json
                for item in actions
            ),
            "full_refund_state": "FULL_REFUND" not in action_types
            or (transaction.funds_status == "REFUNDED" and transaction.order_status == "REFUND_COMPLETED"),
            "partial_refund_state": "PARTIAL_REFUND" not in action_types
            or transaction.funds_status == "PARTIALLY_REFUNDED",
            "release_state": "RELEASE_FUNDS" not in action_types
            or (transaction.funds_status == "RELEASED" and transaction.order_status == "COMPLETED"),
            "return_state": "CREATE_RETURN" not in action_types
            or transaction.order_status in {"RETURN_REQUESTED", "REFUND_COMPLETED"},
            "freeze_state": "FREEZE_FUNDS" not in action_types or transaction.funds_status == "FROZEN",
        }
        return jsonable(
            {
                "verified": all(checks.values()),
                "checks": checks,
                "decision_id": decision.id,
                "transaction_id": transaction.id,
                "order_status": transaction.order_status,
                "funds_status": transaction.funds_status,
            }
        )

    @staticmethod
    def _status_view(
        session: Session,
        dispute: Dispute,
        decision: Decision,
        *,
        idempotent_replay: bool,
    ) -> dict[str, Any]:
        actions = list(
            session.scalars(
                select(ResolutionAction)
                .where(ResolutionAction.decision_id == decision.id)
                .order_by(ResolutionAction.created_at)
            )
        )
        transaction = session.get(Transaction, dispute.transaction_id)
        assert transaction is not None
        return jsonable(
            {
                "case_id": dispute.id,
                "state": dispute.state,
                "state_version": dispute.state_version,
                "decision_id": decision.id,
                "decision_version": decision.version,
                "decision_status": decision.status,
                "transaction": {
                    "transaction_id": transaction.id,
                    "order_status": transaction.order_status,
                    "funds_status": transaction.funds_status,
                },
                "actions": [
                    {
                        "action_id": item.id,
                        "action_type": item.action_type,
                        "amount_minor": item.amount_minor,
                        "idempotency_key": item.idempotency_key,
                        "status": item.status,
                        "external_reference": item.external_reference,
                        "failure_code": item.failure_code,
                        "result": item.result_json,
                    }
                    for item in actions
                ],
                "idempotent_replay": idempotent_replay,
            }
        )
