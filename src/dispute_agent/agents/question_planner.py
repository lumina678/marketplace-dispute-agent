from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.agent_schemas import QuestionProposal
from dispute_agent.db import SessionLocal
from dispute_agent.errors import ConflictError
from dispute_agent.models import AgentOutput, Claim, Evidence
from dispute_agent.services.agent_context import require_run
from dispute_agent.services.tools import ToolService
from dispute_agent.skills import SkillRegistry, get_skill_registry


ROLE_PRIORITY = {
    "EVIDENCE_POLICY_CLERK": 0,
    "BUYER_CASE_ANALYST": 1,
    "SELLER_CASE_ANALYST": 2,
}


class EvidenceGapQuestionPlanner:
    """Merge role proposals and create at most one party's question round."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        tools: ToolService | None = None,
        skill_registry: SkillRegistry | None = None,
    ):
        self.session_factory = session_factory
        self.tools = tools or ToolService(session_factory)
        self.skill_registry = skill_registry or get_skill_registry()

    def plan(self, case_id: str, case_run_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute, run = require_run(session, case_id, case_run_id)
            if dispute.active_case_run_id != run.id or dispute.state != "UNDER_INVESTIGATION":
                raise ConflictError("只能为当前活动调查规划补证问题")
            outputs = list(
                session.scalars(
                    select(AgentOutput).where(
                        AgentOutput.case_run_id == case_run_id,
                        AgentOutput.role.in_(ROLE_PRIORITY),
                    )
                )
            )
            claims = list(session.scalars(select(Claim).where(Claim.dispute_id == case_id)))
            evidence = list(session.scalars(select(Evidence).where(Evidence.dispute_id == case_id)))

        facts = {
            fact.get("field"): fact.get("value")
            for item in evidence
            for fact in item.extracted_facts_json
            if isinstance(fact, dict) and fact.get("field")
        }
        buyer_initial_burden_met = (
            facts.get("promised_memory_gb") is not None
            and facts.get("detected_memory_gb") is not None
            and facts.get("listing_serial") is not None
            and facts.get("listing_serial") == facts.get("detected_serial")
        )
        seller_has_material_claim = any(item.party == "SELLER" and item.material for item in claims)
        bound_skills = {
            (item.skill_name, item.skill_version)
            for item in claims
            if item.material and item.skill_name and item.skill_version
        }
        if len(bound_skills) != 1:
            raise ConflictError("补问 Planner 要求所有关键主张绑定同一个 Skill")
        skill_name, skill_version = next(iter(bound_skills))
        skill = self.skill_registry.get(str(skill_name), str(skill_version))
        if "case.add_open_question" not in set(skill.allowed_tools()):
            raise ConflictError(
                f"Skill {skill.manifest.name}@{skill.manifest.version} 不允许创建外部补问"
            )

        proposals: dict[tuple[str, tuple[str, ...], tuple[str, ...]], tuple[int, QuestionProposal]] = {}
        invalid_count = 0
        policy_filtered_count = 0
        for output in sorted(outputs, key=lambda item: ROLE_PRIORITY[item.role], reverse=True):
            for raw in output.payload_json.get("proposed_questions", []):
                try:
                    proposal = QuestionProposal.model_validate(raw)
                except PydanticValidationError:
                    invalid_count += 1
                    continue
                if (
                    skill.manifest.name == "description-mismatch"
                    and proposal.target == "SELLER"
                    and buyer_initial_burden_met
                    and not seller_has_material_claim
                ):
                    # DM-BURDEN-02 makes preshipment rebuttal evidence relevant only
                    # after the seller actually raises a matching-configuration or
                    # swap defence. Do not let an LLM turn optional rebuttal evidence
                    # into a blocking requirement for an otherwise complete text case.
                    policy_filtered_count += 1
                    continue
                key = (
                    proposal.target,
                    tuple(sorted(set(proposal.resolves_claim_ids))),
                    tuple(sorted({item.upper() for item in proposal.acceptable_evidence_types})),
                )
                # Higher-quality consolidated clerk proposals overwrite equivalent
                # role-specific wording because the loop ends with lower priority.
                proposals[key] = (ROLE_PRIORITY[output.role], proposal)

        if not proposals:
            return {
                "case_id": case_id,
                "case_run_id": case_run_id,
                "selected_target": None,
                "question_ids": [],
                "questions": [],
                "deferred_targets": [],
                "invalid_proposal_count": invalid_count,
                "policy_filtered_proposal_count": policy_filtered_count,
            }

        counts = Counter(key[0] for key in proposals)
        selected_target = sorted(counts, key=lambda target: (-counts[target], target != "BUYER", target))[0]
        results: list[dict[str, Any]] = []
        for key in sorted(proposals):
            if key[0] != selected_target:
                continue
            proposal = proposals[key][1]
            result = self.tools.call(
                "case.add_open_question",
                {
                    "case_id": case_id,
                    "target": proposal.target,
                    "question": proposal.question,
                    "missing_fact": proposal.missing_fact,
                    "resolves_claim_ids": proposal.resolves_claim_ids,
                    "acceptable_evidence_types": proposal.acceptable_evidence_types,
                    "basis_evidence_ids": proposal.basis_evidence_ids,
                    "generation_reason": proposal.generation_reason,
                },
                actor="EVIDENCE_POLICY_CLERK",
                case_run_id=case_run_id,
            )
            if result["status"] == "OPEN" and result["case_run_id"] == case_run_id:
                results.append(result)

        return {
            "case_id": case_id,
            "case_run_id": case_run_id,
            "selected_target": selected_target if results else None,
            "question_ids": [item["question_id"] for item in results],
            "questions": results,
            "deferred_targets": sorted(set(counts) - {selected_target}),
            "invalid_proposal_count": invalid_count,
            "policy_filtered_proposal_count": policy_filtered_count,
        }
