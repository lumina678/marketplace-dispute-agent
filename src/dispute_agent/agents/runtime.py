from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

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
from dispute_agent.models import AgentOutput, Claim, Decision, Dispute, Evidence, OpenQuestion
from dispute_agent.prompts import ROLE_PROMPTS
from dispute_agent.serialization import content_hash
from dispute_agent.services.agent_context import analysis_claim_ids, open_question_payload, require_run
from dispute_agent.services.agent_store import AgentOutputStore
from dispute_agent.services.tools import ToolService
from dispute_agent.skills import DisputeSkill, SkillRegistry, get_skill_registry


CONTEXT_RESOURCE_TOOLS: dict[str, tuple[str, ...]] = {
    "case_state": ("case.get_state",),
    "transaction": ("transaction.get",),
    "listing_snapshot": ("listing.get_snapshot",),
    "conversation_snapshot": ("conversation.search",),
    "shipment_timeline": ("shipment.get_timeline",),
    "claims": ("case.get_state",),
    "evidence": ("evidence.list", "evidence.inspect"),
    "policy_version": ("policy.search",),
    "open_questions": ("case.get_state",),
}


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

    def run_party_analysts(
        self,
        case_id: str,
        case_run_id: str,
        *,
        on_role_complete: Callable[[str, AgentResultEnvelope], None] | None = None,
        on_role_failed: Callable[[str, Exception], None] | None = None,
    ) -> tuple[AgentResultEnvelope, AgentResultEnvelope]:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="party-analyst") as executor:
            futures = {
                executor.submit(self.run_party_analyst, case_id, case_run_id, "BUYER"): "BUYER_CASE_ANALYST",
                executor.submit(self.run_party_analyst, case_id, case_run_id, "SELLER"): "SELLER_CASE_ANALYST",
            }
            results: dict[str, AgentResultEnvelope] = {}
            failures: list[Exception] = []
            for future in as_completed(futures):
                role = futures[future]
                try:
                    output = future.result()
                except Exception as exc:
                    failures.append(exc)
                    if on_role_failed:
                        on_role_failed(role, exc)
                else:
                    results[role] = output
                    if on_role_complete:
                        on_role_complete(role, output)
            if failures:
                raise failures[0]
            return results["BUYER_CASE_ANALYST"], results["SELLER_CASE_ANALYST"]

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

    def assert_case_tool_allowed(self, case_id: str, tool_name: str) -> None:
        """Enforce the persisted Skill tool boundary for orchestrated write steps."""
        with self.session_factory() as session:
            claims = list(
                session.scalars(
                    select(Claim).where(Claim.dispute_id == case_id, Claim.material.is_(True))
                )
            )
        payload = [
            {
                "claim_id": item.id,
                "claim_type": item.claim_type,
                "issue_type": item.issue_type,
                "skill_name": item.skill_name,
                "skill_version": item.skill_version,
            }
            for item in claims
        ]
        skill = self._single_bound_skill(payload, [item.id for item in claims])
        self._require_tool_allowed(skill, tool_name)

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
            persisted_claims = list(
                session.scalars(select(Claim).where(Claim.dispute_id == case_id))
            )

        skill = self._single_bound_skill(
            [self._claim_binding_payload(item) for item in persisted_claims],
            analyzed_ids,
        )
        self._require_tool_allowed(skill, "case.get_state")
        case_state = self.tools.call("case.get_state", {"case_id": case_id}, actor=role, case_run_id=case_run_id)
        observed_skill = self._single_bound_skill(case_state["claims"], analyzed_ids)
        if (observed_skill.manifest.name, observed_skill.manifest.version) != (
            skill.manifest.name,
            skill.manifest.version,
        ):
            raise ConflictError("Skill 绑定在上下文读取期间发生变化，必须重新启动调查")
        required_context = set(skill.required_context())
        unknown_resources = required_context - set(CONTEXT_RESOURCE_TOOLS)
        if unknown_resources:
            raise ConflictError(
                f"Skill {skill.manifest.name}@{skill.manifest.version} 声明了未实现的上下文资源: "
                f"{', '.join(sorted(unknown_resources))}"
            )

        actual_tools = ["case.get_state"]
        actual_resources = {"case_state"}
        result: dict[str, Any] = {
            "case_id": case_id,
            "case_run_id": case_run_id,
            "analyzed_claim_ids": analyzed_ids,
            "case_state": case_state,
            "claims": case_state["claims"],
            "open_questions": case_state.get("open_questions", []),
            "checkpoint_scope": case_state["active_run"]["checkpoint"],
            **self._skill_context(case_state["claims"], analyzed_ids=analyzed_ids),
        }
        if "claims" in required_context:
            actual_resources.add("claims")
        if "open_questions" in required_context:
            actual_resources.add("open_questions")

        def call(tool_name: str, parameters: dict[str, Any]) -> Any:
            self._require_tool_allowed(skill, tool_name)
            actual_tools.append(tool_name)
            return self.tools.call(tool_name, parameters, actor=role, case_run_id=case_run_id)

        if "transaction" in required_context:
            result["transaction"] = call("transaction.get", {"case_id": case_id})
            actual_resources.add("transaction")
        if "listing_snapshot" in required_context:
            result["listing"] = call("listing.get_snapshot", {"case_id": case_id})
            actual_resources.add("listing_snapshot")
        if "conversation_snapshot" in required_context:
            conversation = call("conversation.search", {"case_id": case_id, "query": "", "limit": 100})
            result["messages"] = conversation["messages"]
            actual_resources.add("conversation_snapshot")
        if "shipment_timeline" in required_context:
            shipment = call("shipment.get_timeline", {"case_id": case_id})
            result["shipment"] = shipment["events"]
            actual_resources.add("shipment_timeline")
        if "evidence" in required_context:
            evidence_index = call("evidence.list", {"case_id": case_id})
            selected_evidence = [
                item
                for item in evidence_index["evidence"]
                if not analyzed_ids or set(item.get("related_claim_ids", [])) & set(analyzed_ids)
            ]
            result["evidence"] = [
                call("evidence.inspect", {"case_id": case_id, "evidence_id": item["evidence_id"]})
                for item in selected_evidence
            ]
            actual_resources.add("evidence")
        if include_policy and "policy_version" in required_context:
            result["policy"] = call("policy.search", {"case_id": case_id, "query": "", "limit": 50})
            actual_resources.add("policy_version")

        result["skill_execution"] = {
            "skill_name": skill.manifest.name,
            "skill_version": skill.manifest.version,
            "required_context": list(skill.required_context()),
            "actual_context_resources": sorted(actual_resources),
            "deferred_context_resources": sorted(required_context - actual_resources),
            "allowed_tools": list(skill.allowed_tools()),
            "actual_tool_calls": actual_tools,
        }
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
            persisted_claims = list(
                session.scalars(select(Claim).where(Claim.dispute_id == case_id))
            )

        role = "ADJUDICATION_AGENT"
        skill = self._single_bound_skill(
            [self._claim_binding_payload(item) for item in persisted_claims],
            analyzed_ids,
        )
        self._require_tool_allowed(skill, "case.get_state")
        case_state = self.tools.call("case.get_state", {"case_id": case_id}, actor=role, case_run_id=case_run_id)
        observed_skill = self._single_bound_skill(case_state["claims"], analyzed_ids)
        if (observed_skill.manifest.name, observed_skill.manifest.version) != (
            skill.manifest.name,
            skill.manifest.version,
        ):
            raise ConflictError("Skill 绑定在裁决上下文读取期间发生变化，必须重新启动调查")

        def call(tool_name: str, parameters: dict[str, Any]) -> Any:
            self._require_tool_allowed(skill, tool_name)
            return self.tools.call(tool_name, parameters, actor=role, case_run_id=case_run_id)

        transaction = call("transaction.get", {"case_id": case_id})
        evidence_index = call("evidence.list", {"case_id": case_id})
        selected_evidence = [
            item
            for item in evidence_index["evidence"]
            if not analyzed_ids or set(item.get("related_claim_ids", [])) & set(analyzed_ids)
        ]
        evidence = [
            call(
                "evidence.inspect",
                {"case_id": case_id, "evidence_id": item["evidence_id"]},
            )
            for item in selected_evidence
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
            **self._skill_context(case_state["claims"], analyzed_ids=analyzed_ids),
            "skill_execution": {
                "skill_name": skill.manifest.name,
                "skill_version": skill.manifest.version,
                "required_context": ["case_state", "transaction", "claims", "evidence", "open_questions"],
                "actual_context_resources": ["case_state", "claims", "evidence", "open_questions", "transaction"],
                "allowed_tools": list(skill.allowed_tools()),
                "actual_tool_calls": ["case.get_state", "transaction.get", "evidence.list"]
                + ["evidence.inspect" for _ in evidence],
            },
        }

    def _skill_context(
        self,
        claims: list[dict[str, Any]],
        *,
        analyzed_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        bindings: list[dict[str, Any]] = []
        manifests: dict[tuple[str, str], dict[str, Any]] = {}
        analyzed = set(analyzed_ids or [])
        for claim in claims:
            if analyzed and claim["claim_id"] not in analyzed:
                continue
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

    def _single_bound_skill(
        self,
        claims: list[dict[str, Any]],
        analyzed_ids: list[str],
    ) -> DisputeSkill:
        context = self._skill_context(claims, analyzed_ids=analyzed_ids)
        manifests = context["bound_skills"]
        if len(manifests) != 1:
            raise ConflictError("当前自动调查只允许一个已确认 Skill；复合案件必须拆分 Claim 或转人工")
        manifest = manifests[0]
        return self.skill_registry.get(manifest["name"], manifest["version"])

    @staticmethod
    def _claim_binding_payload(claim: Claim) -> dict[str, Any]:
        return {
            "claim_id": claim.id,
            "party": claim.party,
            "claim_type": claim.claim_type,
            "issue_type": claim.issue_type,
            "issue_subtype": claim.issue_subtype,
            "routing_status": claim.routing_status,
            "routing_source": claim.routing_source,
            "routing_reason": claim.routing_reason,
            "routing_confidence": claim.routing_confidence,
            "skill_name": claim.skill_name,
            "skill_version": claim.skill_version,
        }

    @staticmethod
    def _require_tool_allowed(skill: DisputeSkill, tool_name: str) -> None:
        if tool_name not in set(skill.allowed_tools()):
            raise ConflictError(
                f"Skill {skill.manifest.name}@{skill.manifest.version} 不允许调用工具 {tool_name}"
            )

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
