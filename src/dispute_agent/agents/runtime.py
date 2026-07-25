from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.agent_schemas import (
    AgentResultEnvelope,
    DecisionRecommendation,
    EvidencePolicyReport,
    PartyAnalysis,
)
from dispute_agent.agents.backend import StructuredGenerationBackend, create_generation_backend
from dispute_agent.agents.heuristics import (
    build_decision_recommendation,
    build_evidence_policy_report,
    build_party_analysis,
)
from dispute_agent.db import SessionLocal
from dispute_agent.errors import ConflictError, NotFoundError
from dispute_agent.models import AgentOutput, Decision, Dispute, Evidence, OpenQuestion
from dispute_agent.prompts import ROLE_PROMPTS
from dispute_agent.serialization import content_hash
from dispute_agent.services.agent_context import analysis_claim_ids, open_question_payload, require_run
from dispute_agent.services.agent_store import AgentOutputStore
from dispute_agent.services.tools import ToolService
from dispute_agent.skills import SkillRegistry, get_skill_registry


class AgentRuntime:
    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        backend: StructuredGenerationBackend | None = None,
        tools: ToolService | None = None,
        skill_registry: SkillRegistry | None = None,
    ):
        self.session_factory = session_factory
        self.backend = backend or create_generation_backend()
        self.tools = tools or ToolService(session_factory)
        self.skill_registry = skill_registry or get_skill_registry()
        self.store = AgentOutputStore()

    def run_party_analysts(self, case_id: str, case_run_id: str) -> tuple[AgentResultEnvelope, AgentResultEnvelope]:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="party-analyst") as executor:
            buyer_future = executor.submit(self.run_party_analyst, case_id, case_run_id, "BUYER")
            seller_future = executor.submit(self.run_party_analyst, case_id, case_run_id, "SELLER")
            return buyer_future.result(), seller_future.result()

    def run_party_analyst(self, case_id: str, case_run_id: str, party: str) -> AgentResultEnvelope:
        role = "BUYER_CASE_ANALYST" if party == "BUYER" else "SELLER_CASE_ANALYST"
        existing = self._existing_output(case_id, case_run_id, role, "PARTY_ANALYSIS")
        if existing is not None:
            return existing
        input_data = self._build_investigation_context(case_id, case_run_id, role=role, include_policy=False)
        input_data["party"] = party
        return self._generate_and_store(
            case_id=case_id,
            case_run_id=case_run_id,
            role=role,
            output_type="PARTY_ANALYSIS",
            output_model=PartyAnalysis,
            input_data=input_data,
            builder=build_party_analysis,
        )

    def run_evidence_clerk(self, case_id: str, case_run_id: str) -> AgentResultEnvelope:
        role = "EVIDENCE_POLICY_CLERK"
        existing = self._existing_output(case_id, case_run_id, role, "EVIDENCE_POLICY_REPORT")
        if existing is not None:
            return existing
        input_data = self._build_investigation_context(case_id, case_run_id, role=role, include_policy=True)
        return self._generate_and_store(
            case_id=case_id,
            case_run_id=case_run_id,
            role=role,
            output_type="EVIDENCE_POLICY_REPORT",
            output_model=EvidencePolicyReport,
            input_data=input_data,
            builder=build_evidence_policy_report,
        )

    def run_adjudicator(self, case_id: str, case_run_id: str) -> AgentResultEnvelope:
        role = "ADJUDICATION_AGENT"
        existing = self._existing_output(case_id, case_run_id, role, "DECISION_RECOMMENDATION")
        if existing is not None:
            return existing
        input_data = self._build_adjudication_context(case_id, case_run_id)
        return self._generate_and_store(
            case_id=case_id,
            case_run_id=case_run_id,
            role=role,
            output_type="DECISION_RECOMMENDATION",
            output_model=DecisionRecommendation,
            input_data=input_data,
            builder=build_decision_recommendation,
        )

    def get_output(self, output_id: str) -> AgentResultEnvelope:
        with self.session_factory() as session:
            output = session.get(AgentOutput, output_id)
            if output is None:
                raise NotFoundError(f"Agent 输出不存在: {output_id}")
            return self.store.envelope(output)

    def list_outputs(self, case_id: str, case_run_id: str | None = None) -> list[AgentResultEnvelope]:
        with self.session_factory() as session:
            statement = select(AgentOutput).where(AgentOutput.dispute_id == case_id)
            if case_run_id:
                statement = statement.where(AgentOutput.case_run_id == case_run_id)
            outputs = session.scalars(statement.order_by(AgentOutput.created_at))
            return [self.store.envelope(output) for output in outputs]

    def _build_investigation_context(
        self,
        case_id: str,
        case_run_id: str,
        *,
        role: str,
        include_policy: bool,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute, run = require_run(session, case_id, case_run_id)
            if dispute.active_case_run_id != run.id or dispute.state != "UNDER_INVESTIGATION":
                raise ConflictError("Agent 只能处理当前活动且处于调查状态的 case_run")
            analyzed_ids = analysis_claim_ids(dispute, run)

        case_state = self.tools.call("case.get_state", {"case_id": case_id}, actor=role, case_run_id=case_run_id)
        transaction = self.tools.call("transaction.get", {"case_id": case_id}, actor=role, case_run_id=case_run_id)
        listing = self.tools.call("listing.get_snapshot", {"case_id": case_id}, actor=role, case_run_id=case_run_id)
        conversation = self.tools.call(
            "conversation.search",
            {"case_id": case_id, "query": "", "limit": 100},
            actor=role,
            case_run_id=case_run_id,
        )
        shipment = self.tools.call("shipment.get_timeline", {"case_id": case_id}, actor=role, case_run_id=case_run_id)
        evidence_index = self.tools.call("evidence.list", {"case_id": case_id}, actor=role, case_run_id=case_run_id)
        selected_evidence = [
            item
            for item in evidence_index["evidence"]
            if not analyzed_ids or set(item.get("related_claim_ids", [])) & set(analyzed_ids)
        ]
        evidence = [
            self.tools.call(
                "evidence.inspect",
                {"case_id": case_id, "evidence_id": item["evidence_id"]},
                actor=role,
                case_run_id=case_run_id,
            )
            for item in selected_evidence
        ]
        result: dict[str, Any] = {
            "case_id": case_id,
            "case_run_id": case_run_id,
            "analyzed_claim_ids": analyzed_ids,
            "case_state": case_state,
            "transaction": transaction,
            "listing": listing,
            "messages": conversation["messages"],
            "shipment": shipment["events"],
            "claims": case_state["claims"],
            "evidence": evidence,
            "checkpoint_scope": case_state["active_run"]["checkpoint"],
            **self._skill_context(case_state["claims"]),
        }
        if include_policy:
            result["policy"] = self.tools.call(
                "policy.search",
                {"case_id": case_id, "query": "", "limit": 50},
                actor=role,
                case_run_id=case_run_id,
            )
        return result

    def _build_adjudication_context(self, case_id: str, case_run_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute, run = require_run(session, case_id, case_run_id)
            if dispute.active_case_run_id != run.id or dispute.state != "UNDER_INVESTIGATION":
                raise ConflictError("裁决 Agent 只能处理当前活动调查")
            analyzed_ids = analysis_claim_ids(dispute, run)
            outputs = list(
                session.scalars(
                    select(AgentOutput).where(
                        AgentOutput.case_run_id == case_run_id,
                        AgentOutput.role.in_(["BUYER_CASE_ANALYST", "SELLER_CASE_ANALYST", "EVIDENCE_POLICY_CLERK"]),
                    )
                )
            )
            by_role = {output.role: output.payload_json for output in outputs}
            required_roles = {"BUYER_CASE_ANALYST", "SELLER_CASE_ANALYST", "EVIDENCE_POLICY_CLERK"}
            if not required_roles.issubset(by_role):
                raise ConflictError("裁决前缺少双方分析或证据政策报告")
            previous = session.scalar(
                select(Decision).where(Decision.dispute_id == case_id).order_by(Decision.version.desc()).limit(1)
            )
            questions = open_question_payload(session, case_id)

        case_state = self.tools.call("case.get_state", {"case_id": case_id}, actor="ADJUDICATION_AGENT", case_run_id=case_run_id)
        transaction = self.tools.call("transaction.get", {"case_id": case_id}, actor="ADJUDICATION_AGENT", case_run_id=case_run_id)
        evidence_index = self.tools.call(
            "evidence.list", {"case_id": case_id}, actor="ADJUDICATION_AGENT", case_run_id=case_run_id
        )
        evidence = [
            self.tools.call(
                "evidence.inspect",
                {"case_id": case_id, "evidence_id": item["evidence_id"]},
                actor="ADJUDICATION_AGENT",
                case_run_id=case_run_id,
            )
            for item in evidence_index["evidence"]
        ]
        return {
            "case_id": case_id,
            "case_run_id": case_run_id,
            "analyzed_claim_ids": analyzed_ids,
            "case_state": case_state,
            "transaction": transaction,
            "claims": case_state["claims"],
            "evidence": evidence,
            "buyer_analysis": by_role["BUYER_CASE_ANALYST"],
            "seller_analysis": by_role["SELLER_CASE_ANALYST"],
            "evidence_report": by_role["EVIDENCE_POLICY_CLERK"],
            "open_questions": questions,
            "previous_decision_id": previous.id if previous else None,
            "previous_decision": previous.payload_json if previous else None,
            **self._skill_context(case_state["claims"]),
        }

    def _skill_context(self, claims: list[dict[str, Any]]) -> dict[str, Any]:
        bindings: list[dict[str, Any]] = []
        manifests: dict[tuple[str, str], dict[str, Any]] = {}
        for claim in claims:
            skill = self.skill_registry.resolve_bound_skill(
                name=claim.get("skill_name"),
                version=claim.get("skill_version"),
            )
            binding = {
                "claim_id": claim["claim_id"],
                "issue_type": claim.get("issue_type"),
                "issue_subtype": claim.get("issue_subtype"),
                "routing_status": claim.get("routing_status"),
                "routing_source": claim.get("routing_source"),
                "routing_reason": claim.get("routing_reason"),
                "routing_confidence": claim.get("routing_confidence"),
                "skill_name": claim.get("skill_name"),
                "skill_version": claim.get("skill_version"),
            }
            if skill is not None:
                if not skill.supports(str(claim.get("issue_type")), str(claim.get("claim_type"))):
                    raise ConflictError(
                        f"Claim {claim['claim_id']} 与绑定 Skill {skill.manifest.name}@{skill.manifest.version} 不兼容"
                    )
                key = (skill.manifest.name, skill.manifest.version)
                manifests[key] = skill.manifest.model_dump(mode="json")
            bindings.append(binding)
        return {
            "claim_skill_bindings": bindings,
            "bound_skills": list(manifests.values()),
        }

    def _existing_output(
        self,
        case_id: str,
        case_run_id: str,
        role: str,
        output_type: str,
    ) -> AgentResultEnvelope | None:
        with self.session_factory() as session:
            output = session.scalar(
                select(AgentOutput)
                .where(
                    AgentOutput.dispute_id == case_id,
                    AgentOutput.case_run_id == case_run_id,
                    AgentOutput.role == role,
                    AgentOutput.output_type == output_type,
                )
                .order_by(AgentOutput.created_at.desc())
                .limit(1)
            )
            return self.store.envelope(output) if output is not None else None

    def _generate_and_store(
        self,
        *,
        case_id: str,
        case_run_id: str,
        role: str,
        output_type: str,
        output_model,
        input_data: dict[str, Any],
        builder,
    ) -> AgentResultEnvelope:
        fingerprint = content_hash(input_data)
        with self.session_factory() as session:
            existing = self.store.find(
                session,
                case_run_id=case_run_id,
                role=role,
                output_type=output_type,
                input_fingerprint=fingerprint,
            )
            if existing:
                return self.store.envelope(existing)
        generation = self.backend.generate(
            role=role,
            system_prompt=ROLE_PROMPTS[role],
            input_data=input_data,
            output_model=output_model,
            deterministic_builder=builder,
        )
        with self.session_factory() as session:
            output = self.store.persist(
                session,
                case_id=case_id,
                case_run_id=case_run_id,
                role=role,
                output_type=output_type,
                payload=generation.payload,
                input_data=input_data,
                model_name=generation.model_name,
                usage=generation.usage,
                input_fingerprint=fingerprint,
            )
            session.commit()
            return self.store.envelope(output)
