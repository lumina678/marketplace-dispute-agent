from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.config import Settings, get_settings
from dispute_agent.db import SessionLocal
from dispute_agent.errors import ConflictError, NotFoundError, ValidationError
from dispute_agent.ids import new_id
from dispute_agent.models import (
    Appeal,
    CaseEvent,
    CaseRun,
    Claim,
    Decision,
    Dispute,
    Evidence,
    User,
    utc_now,
)
from dispute_agent.serialization import content_hash, jsonable
from dispute_agent.services.policy import PolicyService
from dispute_agent.services.state_machine import StateMachineService


APPEAL_GROUNDS = {"NEW_EVIDENCE", "POLICY_ERROR", "EVIDENCE_OMISSION", "EXECUTION_ERROR", "OTHER"}


class AppealService:
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

    def submit(
        self,
        case_id: str,
        *,
        appellant_id: str,
        appellant_role: str,
        grounds: str,
        statement: str,
        evidence_ids: list[str] | None = None,
        new_evidence: list[dict[str, Any]] | None = None,
        now: datetime | None = None,
        recorded_by_id: str | None = None,
    ) -> dict[str, Any]:
        appellant_role = appellant_role.upper()
        grounds = grounds.upper()
        now = now or utc_now()
        if appellant_role not in {"BUYER", "SELLER"}:
            raise ValidationError("appellant_role 必须为 BUYER 或 SELLER")
        if grounds not in APPEAL_GROUNDS:
            raise ValidationError("未知申诉理由类型")
        if not statement.strip():
            raise ValidationError("申诉说明不能为空")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidationError("now 必须包含时区")

        with self.session_factory() as session:
            recorder = (
                self._require_reviewer(session, recorded_by_id, allow_admin=True)
                if recorded_by_id
                else None
            )
            dispute = session.get(Dispute, case_id)
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            if dispute.state != "RESOLVED":
                raise ConflictError("只有 RESOLVED 案件可以提交申诉")
            transaction = dispute.transaction
            expected_user_id = transaction.buyer_id if appellant_role == "BUYER" else transaction.seller_id
            if appellant_id != expected_user_id:
                raise ValidationError("申诉人身份与交易角色不匹配")
            if session.get(User, appellant_id) is None:
                raise NotFoundError("申诉用户不存在")
            open_appeal = session.scalar(
                select(Appeal).where(
                    Appeal.dispute_id == case_id,
                    Appeal.status.in_(["SUBMITTED", "UNDER_REVIEW"]),
                )
            )
            if open_appeal is not None:
                raise ConflictError("案件已经存在开放申诉")
            decision = self._latest_decision(session, case_id)
            if decision is None or decision.status != "APPROVED":
                raise ConflictError("案件没有可申诉的已批准决定")

            policy = self._ensure_policy(session, dispute)
            resolved_event = session.scalar(
                select(CaseEvent)
                .where(CaseEvent.dispute_id == case_id, CaseEvent.to_state == "RESOLVED")
                .order_by(CaseEvent.sequence.desc())
                .limit(1)
            )
            if resolved_event is None:
                raise ConflictError("案件缺少可审计的解决时间")
            deadline_hours = self._appeal_deadline_hours(policy.document_json)
            deadline = resolved_event.occurred_at + timedelta(hours=deadline_hours)
            if now > deadline:
                raise ConflictError("申诉已超过政策规定期限")

            selected_evidence_ids = set(evidence_ids or [])
            selected_evidence_ids.update(
                self._persist_new_evidence(
                    session,
                    dispute,
                    appellant_role=appellant_role,
                    items=new_evidence or [],
                    submitted_at=now,
                    recorded_by_id=recorder.id if recorder else None,
                )
            )
            known_evidence_ids = set(session.scalars(select(Evidence.id).where(Evidence.dispute_id == case_id)))
            if not selected_evidence_ids.issubset(known_evidence_ids):
                raise ValidationError("evidence_ids 必须属于当前案件")
            if grounds == "NEW_EVIDENCE" and not selected_evidence_ids:
                raise ValidationError("NEW_EVIDENCE 申诉必须提交至少一项证据")

            appeal = Appeal(
                id=new_id("appeal"),
                dispute_id=case_id,
                decision_id=decision.id,
                appellant_id=appellant_id,
                appellant_role=appellant_role,
                recorded_by_id=recorder.id if recorder else None,
                grounds=grounds,
                statement=statement.strip(),
                evidence_ids_json=sorted(selected_evidence_ids),
                policy_id=policy.policy_id,
                policy_version=policy.version,
                decision_content_sha256=decision.content_sha256,
                deadline=deadline,
                status="SUBMITTED",
                submitted_at=now,
            )
            session.add(appeal)
            session.flush()
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="VALID_APPEAL_RECEIVED",
                actor_type=appellant_role,
                actor_id=appellant_id,
                reason_code="APPEAL_SUBMITTED_WITHIN_POLICY_WINDOW",
                expected_state_version=dispute.state_version,
                metadata={
                    "appeal_id": appeal.id,
                    "appeal_within_deadline": True,
                    "deadline": deadline.isoformat(),
                    "grounds": grounds,
                    "recorded_by_id": recorder.id if recorder else None,
                },
            )
            session.refresh(dispute)
            session.commit()
            return self._view(dispute, appeal)

    def accept(
        self,
        case_id: str,
        *,
        appeal_id: str,
        reviewer_id: str,
        reason: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("受理原因不能为空")
        with self.session_factory() as session:
            reviewer = self._require_reviewer(session, reviewer_id, allow_admin=True)
            dispute, appeal = self._appeal_context(session, case_id, appeal_id)
            if appeal.status == "ACCEPTED" and dispute.state == "UNDER_INVESTIGATION":
                run = session.get(CaseRun, dispute.active_case_run_id)
                return {**self._view(dispute, appeal), "case_run_id": run.id if run else None}
            if dispute.state != "APPEALED" or appeal.status not in {"SUBMITTED", "UNDER_REVIEW"}:
                raise ConflictError("申诉当前不可受理重开")
            policy = self._ensure_policy(session, dispute)
            if appeal.policy_id is None or appeal.policy_version is None:
                appeal.policy_id = policy.policy_id
                appeal.policy_version = policy.version
            elif (appeal.policy_id, appeal.policy_version) != (policy.policy_id, policy.version):
                raise ConflictError("申诉固定的政策版本与案件交易时点不一致")
            prior_decision = session.get(Decision, appeal.decision_id)
            if prior_decision is None or prior_decision.dispute_id != case_id:
                raise ConflictError("申诉引用的原决定不存在")
            if appeal.decision_content_sha256 is None:
                appeal.decision_content_sha256 = prior_decision.content_sha256
            elif appeal.decision_content_sha256 != prior_decision.content_sha256:
                raise ConflictError("申诉固定的原决定哈希已不一致")
            eligible = self._material_reopen_basis(session, appeal)
            if not eligible:
                raise ConflictError("申诉没有新证据、证据遗漏、政策错误或执行错误，不能重开")

            appeal.status = "ACCEPTED"
            appeal.resolved_at = utc_now()
            appeal.resolution_reason = reason.strip()
            session.flush()
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="APPEAL_ACCEPTED",
                actor_type=reviewer.role,
                actor_id=reviewer.id,
                reason_code="MATERIAL_APPEAL_BASIS_CONFIRMED",
                expected_state_version=dispute.state_version,
                metadata={
                    "appeal_id": appeal.id,
                    "new_evidence_or_material_error_exists": True,
                    "grounds": appeal.grounds,
                },
            )
            session.refresh(dispute)

            evidence = list(
                session.scalars(select(Evidence).where(Evidence.id.in_(appeal.evidence_ids_json)))
            ) if appeal.evidence_ids_json else []
            dirty_claim_ids = {
                claim_id
                for item in evidence
                for claim_id in item.related_claim_ids_json
            }
            material_claim_ids = set(
                session.scalars(select(Claim.id).where(Claim.dispute_id == case_id, Claim.material.is_(True)))
            )
            dirty_claim_ids &= material_claim_ids
            if not dirty_claim_ids or appeal.grounds in {"POLICY_ERROR", "EXECUTION_ERROR"}:
                dirty_claim_ids = material_claim_ids

            previous_run = session.get(CaseRun, dispute.active_case_run_id) if dispute.active_case_run_id else None
            run_number = (
                session.scalar(select(func.max(CaseRun.run_number)).where(CaseRun.dispute_id == case_id)) or 0
            ) + 1
            checkpoint = {
                "schema_version": "1.0.0",
                "appeal_id": appeal.id,
                "resumed_from_run_id": previous_run.id if previous_run else None,
                "prior_decision_id": appeal.decision_id,
                "new_evidence_ids": appeal.evidence_ids_json,
                "dirty_claim_ids": sorted(dirty_claim_ids),
                "completed_phases": ["INTAKE", "SNAPSHOT", "CLAIM_EXTRACTION"],
                "phase_results": {},
                "pending_phase": "PARTY_ANALYSIS",
                "pending_agent_roles": ["BUYER_CASE_ANALYST", "SELLER_CASE_ANALYST"],
                "pause_reason": "APPEAL_REINVESTIGATION_STARTED",
            }
            run = CaseRun(
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
            session.add(run)
            session.flush()
            dispute.active_case_run_id = run.id
            session.flush()
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="REINVESTIGATION_STARTED",
                actor_type="ORCHESTRATOR",
                actor_id="appeal-orchestrator",
                reason_code="APPEAL_CASE_RUN_CREATED",
                expected_state_version=dispute.state_version,
                metadata={
                    "appeal_id": appeal.id,
                    "new_case_run_id": run.id,
                    "prior_decision_id": appeal.decision_id,
                },
            )
            session.refresh(dispute)
            session.commit()
            return {
                **self._view(dispute, appeal),
                "case_run_id": run.id,
                "run_number": run.run_number,
                "phase": run.phase,
                "checkpoint": jsonable(run.checkpoint_json),
            }

    def deny(
        self,
        case_id: str,
        *,
        appeal_id: str,
        reviewer_id: str,
        reason: str,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("驳回原因不能为空")
        with self.session_factory() as session:
            reviewer = self._require_reviewer(session, reviewer_id, allow_admin=False)
            dispute, appeal = self._appeal_context(session, case_id, appeal_id)
            if appeal.status == "DENIED" and dispute.state == "CLOSED":
                return self._view(dispute, appeal)
            if dispute.state != "APPEALED" or appeal.status not in {"SUBMITTED", "UNDER_REVIEW"}:
                raise ConflictError("申诉当前不可驳回")
            appeal.status = "DENIED"
            appeal.resolved_at = utc_now()
            appeal.resolution_reason = reason.strip()
            session.flush()
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="APPEAL_DENIED",
                actor_type="REVIEWER",
                actor_id=reviewer.id,
                reason_code="APPEAL_DID_NOT_JUSTIFY_REOPENING",
                expected_state_version=dispute.state_version,
                metadata={"appeal_id": appeal.id, "resolution_reason": reason.strip()},
            )
            session.refresh(dispute)
            session.commit()
            return self._view(dispute, appeal)

    def close_expired_window(self, case_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or utc_now()
        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            if dispute.state == "CLOSED":
                return {"case_id": case_id, "state": dispute.state, "state_version": dispute.state_version}
            if dispute.state != "RESOLVED":
                raise ConflictError("只有 RESOLVED 案件可以关闭过期申诉窗口")
            policy = self._ensure_policy(session, dispute)
            resolved_event = session.scalar(
                select(CaseEvent)
                .where(CaseEvent.dispute_id == case_id, CaseEvent.to_state == "RESOLVED")
                .order_by(CaseEvent.sequence.desc())
                .limit(1)
            )
            if resolved_event is None:
                raise ConflictError("案件缺少解决时间")
            deadline = resolved_event.occurred_at + timedelta(
                hours=self._appeal_deadline_hours(policy.document_json)
            )
            if now <= deadline:
                raise ConflictError("申诉窗口尚未到期")
            self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="APPEAL_WINDOW_EXPIRED",
                actor_type="SYSTEM",
                actor_id="appeal-window-monitor",
                reason_code="POLICY_APPEAL_WINDOW_EXPIRED",
                expected_state_version=dispute.state_version,
                metadata={"deadline": deadline.isoformat()},
            )
            session.refresh(dispute)
            session.commit()
            return {"case_id": case_id, "state": dispute.state, "state_version": dispute.state_version}

    def list_for_case(self, case_id: str) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            appeals = session.scalars(
                select(Appeal).where(Appeal.dispute_id == case_id).order_by(Appeal.submitted_at)
            )
            return [self._view(dispute, item) for item in appeals]

    def _persist_new_evidence(
        self,
        session: Session,
        dispute: Dispute,
        *,
        appellant_role: str,
        items: list[dict[str, Any]],
        submitted_at: datetime,
        recorded_by_id: str | None,
    ) -> list[str]:
        if not items:
            return []
        known_claim_ids = set(session.scalars(select(Claim.id).where(Claim.dispute_id == dispute.id)))
        result: list[str] = []
        for item in items:
            captured_at = item.get("captured_at")
            if not isinstance(captured_at, datetime) or captured_at.tzinfo is None or captured_at.utcoffset() is None:
                raise ValidationError("新申诉证据 captured_at 必须是带时区时间")
            related_claim_ids = set(item.get("related_claim_ids", []))
            if not related_claim_ids or not related_claim_ids.issubset(known_claim_ids):
                raise ValidationError("新申诉证据必须关联当前案件主张")
            supplied_hash = item.get("content_sha256")
            if supplied_hash is not None and len(str(supplied_hash)) != 64:
                raise ValidationError("content_sha256 必须是 64 位哈希")
            digest = supplied_hash or content_hash(
                {
                    "evidence_type": item["evidence_type"],
                    "description": item["description"],
                    "source_record_id": item["source_record_id"],
                    "captured_at": captured_at,
                    "extracted_facts": item.get("extracted_facts", []),
                }
            )
            evidence = session.scalar(
                select(Evidence).where(
                    Evidence.dispute_id == dispute.id,
                    Evidence.content_sha256 == digest,
                )
            )
            if evidence is None:
                evidence = Evidence(
                    id=new_id("evidence"),
                    dispute_id=dispute.id,
                    submitted_by=appellant_role,
                    recorded_by_id=recorded_by_id,
                    evidence_type=str(item["evidence_type"]).upper(),
                    description=str(item["description"]),
                    source_system=str(item.get("source_system", "APPEAL_EVIDENCE_STORE")),
                    source_record_id=str(item["source_record_id"]),
                    captured_at=captured_at,
                    submitted_at=submitted_at,
                    content_sha256=str(digest),
                    immutable_uri=str(
                        item.get("immutable_uri")
                        or f"evidence://{dispute.id}/appeal/{item['source_record_id']}"
                    ),
                    integrity_status="HASH_VERIFIED" if supplied_hash else "UNVERIFIED",
                    related_claim_ids_json=sorted(related_claim_ids),
                    extracted_facts_json=item.get("extracted_facts", []),
                    handling_flags_json=item.get("handling_flags", []),
                )
                session.add(evidence)
                session.flush()
            result.append(evidence.id)
        return result

    def _ensure_policy(self, session: Session, dispute: Dispute):  # type: ignore[no-untyped-def]
        service = PolicyService(session)
        if dispute.policy_id and dispute.policy_version:
            policy = service.get_version(dispute.policy_id, dispute.policy_version)
            if dispute.policy_basis_time is None:
                dispute.policy_basis_time = dispute.transaction.paid_at
                session.flush()
            return policy
        policy = service.select_for_transaction(
            policy_id="marketplace.description_mismatch",
            paid_at=dispute.transaction.paid_at,
            dispute_type=dispute.dispute_type,
            category=dispute.transaction.category,
        )
        dispute.policy_id = policy.policy_id
        dispute.policy_version = policy.version
        dispute.policy_basis_time = dispute.transaction.paid_at
        session.flush()
        return policy

    @staticmethod
    def _appeal_deadline_hours(document: dict[str, Any]) -> int:
        rule = next((item for item in document["rules"] if item["rule_id"] == "DM-APPEAL-01"), None)
        if rule is None:
            raise ValidationError("政策缺少 DM-APPEAL-01")
        match = re.search(r"(\d+)\s*小时内申诉", rule["text"])
        if match is None:
            raise ValidationError("无法从政策解析申诉期限")
        return int(match.group(1))

    @staticmethod
    def _material_reopen_basis(session: Session, appeal: Appeal) -> bool:
        if appeal.grounds in {"POLICY_ERROR", "EXECUTION_ERROR"}:
            return True
        if appeal.grounds not in {"NEW_EVIDENCE", "EVIDENCE_OMISSION"} or not appeal.evidence_ids_json:
            return False
        decision = session.get(Decision, appeal.decision_id)
        if decision is None:
            return False
        previously_used = {
            evidence_id
            for finding in decision.payload_json.get("claim_findings", [])
            for evidence_id in finding.get("evidence_ids", [])
        }
        previously_used.update(
            evidence_id
            for fact in decision.payload_json.get("established_facts", [])
            for evidence_id in fact.get("evidence_ids", [])
        )
        return bool(set(appeal.evidence_ids_json) - previously_used)

    @staticmethod
    def _require_reviewer(session: Session, reviewer_id: str, *, allow_admin: bool) -> User:
        reviewer = session.get(User, reviewer_id)
        allowed = {"REVIEWER", "ADMIN"} if allow_admin else {"REVIEWER"}
        if reviewer is None or reviewer.role not in allowed:
            raise ValidationError("reviewer_id 没有申诉审核权限")
        return reviewer

    @staticmethod
    def _appeal_context(session: Session, case_id: str, appeal_id: str) -> tuple[Dispute, Appeal]:
        dispute = session.get(Dispute, case_id)
        appeal = session.get(Appeal, appeal_id)
        if dispute is None or appeal is None or appeal.dispute_id != case_id:
            raise NotFoundError("案件或申诉不存在")
        return dispute, appeal

    @staticmethod
    def _latest_decision(session: Session, case_id: str) -> Decision | None:
        return session.scalar(
            select(Decision).where(Decision.dispute_id == case_id).order_by(Decision.version.desc()).limit(1)
        )

    @staticmethod
    def _view(dispute: Dispute, appeal: Appeal) -> dict[str, Any]:
        return jsonable(
            {
                "appeal_id": appeal.id,
                "case_id": appeal.dispute_id,
                "state": dispute.state,
                "state_version": dispute.state_version,
                "decision_id": appeal.decision_id,
                "appellant_id": appeal.appellant_id,
                "appellant_role": appeal.appellant_role,
                "recorded_by_id": appeal.recorded_by_id,
                "grounds": appeal.grounds,
                "statement": appeal.statement,
                "evidence_ids": appeal.evidence_ids_json,
                "policy": {"policy_id": appeal.policy_id, "version": appeal.policy_version},
                "decision_content_sha256": appeal.decision_content_sha256,
                "status": appeal.status,
                "submitted_at": appeal.submitted_at,
                "deadline": appeal.deadline,
                "resolved_at": appeal.resolved_at,
                "resolution_reason": appeal.resolution_reason,
            }
        )
