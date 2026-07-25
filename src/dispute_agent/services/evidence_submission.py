from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.db import SessionLocal
from dispute_agent.errors import ConflictError, NotFoundError, ValidationError
from dispute_agent.ids import new_id
from dispute_agent.models import Claim, Dispute, Evidence, OpenQuestion, utc_now
from dispute_agent.serialization import content_hash, jsonable
from dispute_agent.services.orchestrator import CaseOrchestrator


class EvidenceSubmissionService:
    """Persist immutable evidence metadata, link it to questions, then resume."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        orchestrator: CaseOrchestrator | None = None,
    ):
        self.session_factory = session_factory
        self.orchestrator = orchestrator or CaseOrchestrator(session_factory)

    def submit_and_resume(
        self,
        case_id: str,
        *,
        target: str,
        question_ids: list[str],
        evidence_type: str,
        description: str,
        source_record_id: str,
        captured_at: datetime,
        extracted_facts: list[dict[str, Any]],
        related_claim_ids: list[str] | None = None,
        content_sha256: str | None = None,
        immutable_uri: str | None = None,
        source_system: str = "EVIDENCE_STORE",
        handling_flags: list[str] | None = None,
        actor_id: str = "orchestrator",
    ) -> dict[str, Any]:
        target = target.upper()
        if target not in {"BUYER", "SELLER"}:
            raise ValidationError("target 必须为 BUYER 或 SELLER")
        if not question_ids:
            raise ValidationError("question_ids 不能为空")
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValidationError("captured_at 必须包含时区")
        if content_sha256 is not None and len(content_sha256) != 64:
            raise ValidationError("content_sha256 必须是 64 位十六进制哈希")

        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            expected_state = "WAITING_FOR_BUYER" if target == "BUYER" else "WAITING_FOR_SELLER"
            if dispute.state != expected_state:
                raise ConflictError(f"案件不在 {target} 补证等待状态")
            questions = list(
                session.scalars(
                    select(OpenQuestion).where(
                        OpenQuestion.dispute_id == case_id,
                        OpenQuestion.target == target,
                        OpenQuestion.status == "OPEN",
                        OpenQuestion.id.in_(question_ids),
                    )
                )
            )
            if len(questions) != len(set(question_ids)):
                raise ValidationError("question_ids 必须全部引用该方的开放问题")
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

            question_claim_ids = {
                claim_id
                for question in questions
                for claim_id in question.resolves_claim_ids_json
            }
            selected_claim_ids = set(related_claim_ids or question_claim_ids)
            known_claim_ids = set(session.scalars(select(Claim.id).where(Claim.dispute_id == case_id)))
            if not selected_claim_ids or not selected_claim_ids.issubset(known_claim_ids):
                raise ValidationError("related_claim_ids 必须引用当前案件主张")
            if not question_claim_ids.issubset(selected_claim_ids):
                raise ValidationError("新证据必须关联其回答问题所覆盖的主张")

            digest = content_sha256 or content_hash(
                {
                    "evidence_type": evidence_type,
                    "description": description,
                    "source_system": source_system,
                    "source_record_id": source_record_id,
                    "captured_at": captured_at,
                    "extracted_facts": extracted_facts,
                }
            )
            existing = session.scalar(
                select(Evidence).where(
                    Evidence.dispute_id == case_id,
                    Evidence.content_sha256 == digest,
                )
            )
            created = existing is None
            evidence = existing
            if evidence is None:
                evidence = Evidence(
                    id=new_id("evidence"),
                    dispute_id=case_id,
                    submitted_by=target,
                    evidence_type=evidence_type.upper(),
                    description=description,
                    source_system=source_system,
                    source_record_id=source_record_id,
                    captured_at=captured_at,
                    submitted_at=utc_now(),
                    content_sha256=digest,
                    immutable_uri=immutable_uri or f"evidence://{case_id}/{source_record_id}",
                    integrity_status="HASH_VERIFIED" if content_sha256 else "UNVERIFIED",
                    related_claim_ids_json=sorted(selected_claim_ids),
                    extracted_facts_json=extracted_facts,
                    handling_flags_json=handling_flags or [],
                )
                session.add(evidence)
                session.commit()
            evidence_view = jsonable(
                {
                    "evidence_id": evidence.id,
                    "case_id": case_id,
                    "submitted_by": evidence.submitted_by,
                    "evidence_type": evidence.evidence_type,
                    "content_sha256": evidence.content_sha256,
                    "immutable_uri": evidence.immutable_uri,
                    "integrity_status": evidence.integrity_status,
                    "related_claim_ids": evidence.related_claim_ids_json,
                    "created": created,
                }
            )

        run = self.orchestrator.resume_with_evidence(
            case_id,
            target=target,
            evidence_id=evidence.id,
            question_ids=question_ids,
            actor_id=actor_id,
        )
        return {"evidence": evidence_view, "run": run}
