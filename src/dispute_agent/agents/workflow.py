from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.agents.question_planner import EvidenceGapQuestionPlanner
from dispute_agent.agents.runtime import AgentRuntime
from dispute_agent.db import SessionLocal
from dispute_agent.errors import ConflictError
from dispute_agent.services.orchestrator import CaseOrchestrator
from dispute_agent.services.decision_guard import DecisionGuard
from dispute_agent.services.tools import ToolService


class InvestigationWorkflow:
    """Run steps 8–10 until evidence, guard or human review blocks progress."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        orchestrator: CaseOrchestrator | None = None,
        runtime: AgentRuntime | None = None,
        tools: ToolService | None = None,
        question_planner: EvidenceGapQuestionPlanner | None = None,
        decision_guard: DecisionGuard | None = None,
    ):
        self.session_factory = session_factory
        self.tools = tools or ToolService(session_factory)
        self.orchestrator = orchestrator or CaseOrchestrator(session_factory)
        self.runtime = runtime or AgentRuntime(session_factory, tools=self.tools)
        self.question_planner = question_planner or EvidenceGapQuestionPlanner(
            session_factory,
            tools=self.tools,
        )
        self.decision_guard = decision_guard or DecisionGuard(session_factory)

    def run_until_blocked(self, case_id: str, *, actor_id: str = "investigation-workflow") -> dict[str, Any]:
        view = self.orchestrator.start(case_id, actor_id=actor_id)
        for _ in range(12):
            state = view["state"]
            phase = view["phase"]
            if state in {"WAITING_FOR_BUYER", "WAITING_FOR_SELLER"}:
                return self._with_boundary(view, "EVIDENCE_RESPONSE_REQUIRED")
            if state == "HUMAN_REVIEW" or phase == "HUMAN_REVIEW":
                return self._with_boundary(view, "HUMAN_REVIEW_REQUIRED")
            if state != "UNDER_INVESTIGATION":
                return self._with_boundary(view, "CASE_NOT_AUTOMATABLE")

            run_id = view["case_run_id"]
            if phase == "PARTY_ANALYSIS":
                buyer, seller = self.runtime.run_party_analysts(case_id, run_id)
                view = self.orchestrator.submit_phase_result(
                    case_id,
                    case_run_id=run_id,
                    phase=phase,
                    payload={
                        "buyer_analysis": self._output_reference(buyer),
                        "seller_analysis": self._output_reference(seller),
                    },
                    actor=actor_id,
                    tokens_used=self._tokens(buyer) + self._tokens(seller),
                )
                continue

            if phase == "EVIDENCE_REVIEW":
                report = self.runtime.run_evidence_clerk(case_id, run_id)
                view = self.orchestrator.submit_phase_result(
                    case_id,
                    case_run_id=run_id,
                    phase=phase,
                    payload={"evidence_policy_report": self._output_reference(report)},
                    actor=actor_id,
                    tokens_used=self._tokens(report),
                )
                continue

            if phase == "GAP_RESOLUTION":
                question_plan = self.question_planner.plan(case_id, run_id)
                view = self.orchestrator.submit_phase_result(
                    case_id,
                    case_run_id=run_id,
                    phase=phase,
                    payload=question_plan,
                    actor=actor_id,
                )
                continue

            if phase == "ADJUDICATION":
                recommendation = self.runtime.run_adjudicator(case_id, run_id)
                payload = recommendation.payload
                draft = self.tools.call(
                    "resolution.create_draft",
                    {
                        "case_id": case_id,
                        "source_agent_output_id": recommendation.output_id,
                        "outcome": payload["outcome"],
                        "refund_amount_minor": payload.get("refund_amount_minor"),
                        "shipping_payer": payload["shipping_payer"],
                        "actions": payload.get("proposed_actions", []),
                        "payload": payload,
                    },
                    actor="ADJUDICATION_AGENT",
                    case_run_id=run_id,
                )
                view = self.orchestrator.submit_phase_result(
                    case_id,
                    case_run_id=run_id,
                    phase=phase,
                    payload={
                        "decision_id": draft["decision_id"],
                        "agent_output_id": recommendation.output_id,
                        "decision_content_sha256": draft["content_sha256"],
                    },
                    actor=actor_id,
                    tokens_used=self._tokens(recommendation),
                )
                continue

            if phase == "GUARD_CHECK":
                guard_result = self.decision_guard.evaluate(
                    case_id,
                    run_id,
                    decision_id=view["checkpoint"].get("decision_id"),
                )
                guard_payload = guard_result.model_dump(mode="json")
                view = self.orchestrator.submit_phase_result(
                    case_id,
                    case_run_id=run_id,
                    phase=phase,
                    payload=guard_payload,
                    actor=actor_id,
                )
                if not guard_result.passed:
                    return {
                        **view,
                        "workflow_boundary": "GUARD_REMEDIATION_REQUIRED",
                        "guard_result": guard_payload,
                    }
                continue

            raise ConflictError(f"工作流没有步骤 8–10 的处理器: {phase}")
        raise ConflictError("步骤 8–10 工作流超过最大内部推进次数")

    @staticmethod
    def _tokens(output) -> int:  # type: ignore[no-untyped-def]
        return max(0, int(output.usage.get("total_tokens_estimate", 0)))

    @staticmethod
    def _output_reference(output) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        return {
            "output_id": output.output_id,
            "role": output.role,
            "output_type": output.output_type,
            "content_sha256": output.content_sha256,
        }

    @staticmethod
    def _with_boundary(view: dict[str, Any], boundary: str) -> dict[str, Any]:
        return {**view, "workflow_boundary": boundary}
