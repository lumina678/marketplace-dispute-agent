from __future__ import annotations

from sqlalchemy import func, select

from dispute_agent.agents.backend import CallableStructuredBackend
from dispute_agent.agents.heuristics import (
    build_decision_recommendation,
    build_evidence_policy_report,
    build_party_analysis,
)
from dispute_agent.agents.runtime import AgentRuntime
from dispute_agent.agents.workflow import InvestigationWorkflow
from dispute_agent.models import AgentOutput, CaseRun, Decision, Dispute, Evidence, Message
from dispute_agent.services.demo_cases import DemoCaseService


def test_clear_mismatch_seed_is_text_only_and_contains_both_party_chat(context) -> None:
    with context.sessions() as session:
        messages = list(
            session.scalars(
                select(Message)
                .where(Message.dispute_id == "case_clear_mismatch")
                .order_by(Message.sent_at)
            )
        )
        evidence = list(
            session.scalars(select(Evidence).where(Evidence.dispute_id == "case_clear_mismatch"))
        )
    assert len(messages) == 3
    assert {item.sender_role for item in messages} == {"BUYER", "SELLER"}
    assert all(item.snapshot_locked for item in messages)
    assert {item.evidence_type for item in evidence} == {"LISTING_SNAPSHOT", "DOCUMENT"}
    assert not {"PHOTO", "VIDEO"} & {item.evidence_type for item in evidence}


def test_policy_filter_prevents_optional_seller_media_request_from_blocking_text_case(context) -> None:
    def generate_json(role, _prompt, input_data, _schema):  # type: ignore[no-untyped-def]
        if role in {"BUYER_CASE_ANALYST", "SELLER_CASE_ANALYST"}:
            payload = build_party_analysis(input_data).model_dump(mode="json")
        elif role == "EVIDENCE_POLICY_CLERK":
            payload = build_evidence_policy_report(input_data).model_dump(mode="json")
        else:
            return build_decision_recommendation(input_data).model_dump(mode="json")
        payload["proposed_questions"].append(
            {
                "target": "SELLER",
                "question": "请补充发货前图片或视频。",
                "missing_fact": "卖方发货前配置",
                "resolves_claim_ids": ["claim_clear_mismatch_buyer"],
                "acceptable_evidence_types": ["PHOTO", "VIDEO"],
                "basis_evidence_ids": ["ev_clear_mismatch_listing"],
                "generation_reason": "MATERIAL_EVIDENCE_GAP",
            }
        )
        return payload

    backend = CallableStructuredBackend("text-demo-test-model", generate_json)
    runtime = AgentRuntime(context.sessions, backend=backend, tools=context.tools)
    result = InvestigationWorkflow(
        context.sessions,
        orchestrator=context.orchestrator,
        runtime=runtime,
        tools=context.tools,
    ).run_until_blocked("case_clear_mismatch")

    assert result["state"] == "HUMAN_REVIEW"
    gap = result["checkpoint"]["phase_results"]["GAP_RESOLUTION"]
    assert gap["question_ids"] == []
    assert gap["policy_filtered_proposal_count"] >= 1


def test_demo_reset_restores_repeatable_text_case(context) -> None:
    workflow = InvestigationWorkflow(
        context.sessions,
        orchestrator=context.orchestrator,
        runtime=AgentRuntime(context.sessions, tools=context.tools),
        tools=context.tools,
    )
    assert workflow.run_until_blocked("case_clear_mismatch")["state"] == "HUMAN_REVIEW"

    result = DemoCaseService(context.sessions).reset_text_demo("case_clear_mismatch")
    assert result["state"] == "SUBMITTED"
    assert result["ready"] is True
    assert result["message_count"] == 3
    assert result["evidence_count"] == 2

    with context.sessions() as session:
        dispute = session.get(Dispute, "case_clear_mismatch")
        assert dispute is not None
        assert dispute.active_case_run_id is None
        assert dispute.policy_id is None
        assert session.scalar(
            select(func.count()).select_from(CaseRun).where(CaseRun.dispute_id == dispute.id)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(AgentOutput).where(AgentOutput.dispute_id == dispute.id)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(Decision).where(Decision.dispute_id == dispute.id)
        ) == 0
