from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from dispute_agent.errors import NotFoundError
from dispute_agent.models import AgentOutput, CaseRun, Claim, Dispute, OpenQuestion


def require_run(session: Session, case_id: str, case_run_id: str) -> tuple[Dispute, CaseRun]:
    dispute = session.get(Dispute, case_id)
    run = session.get(CaseRun, case_run_id)
    if dispute is None or run is None or run.dispute_id != case_id:
        raise NotFoundError("案件或 case_run 不存在")
    return dispute, run


def analysis_claim_ids(dispute: Dispute, run: CaseRun) -> list[str]:
    dirty = set(run.checkpoint_json.get("dirty_claim_ids", []))
    material_ids = {claim.id for claim in dispute.claims if claim.material}
    selected = dirty & material_ids if dirty else set(material_ids)
    # Reevaluate both sides of a directly linked claim/response pair. This keeps
    # a narrow dirty scope without dropping the opposing material assertion.
    if dirty:
        changed = True
        while changed:
            changed = False
            for claim in dispute.claims:
                if not claim.material:
                    continue
                if claim.id in selected and claim.responds_to_claim_id in material_ids:
                    if claim.responds_to_claim_id not in selected:
                        selected.add(claim.responds_to_claim_id)
                        changed = True
                elif claim.responds_to_claim_id in selected and claim.id not in selected:
                    selected.add(claim.id)
                    changed = True
    return sorted(selected)


def latest_outputs(
    session: Session,
    *,
    case_run_id: str,
    roles: list[str] | None = None,
) -> list[AgentOutput]:
    statement = select(AgentOutput).where(AgentOutput.case_run_id == case_run_id)
    if roles:
        statement = statement.where(AgentOutput.role.in_(roles))
    return list(session.scalars(statement.order_by(AgentOutput.created_at)))


def open_question_payload(session: Session, case_id: str) -> list[dict[str, Any]]:
    return [
        {
            "question_id": item.id,
            "target": item.target,
            "question": item.question,
            "missing_fact": item.missing_fact,
            "resolves_claim_ids": item.resolves_claim_ids_json,
            "acceptable_evidence_types": item.acceptable_evidence_types_json,
            "basis_evidence_ids": item.basis_evidence_ids_json,
            "dedupe_key": item.dedupe_key,
            "generation_reason": item.generation_reason,
            "round_number": item.round_number,
            "status": item.status,
        }
        for item in session.scalars(
            select(OpenQuestion).where(OpenQuestion.dispute_id == case_id).order_by(OpenQuestion.created_at)
        )
    ]
