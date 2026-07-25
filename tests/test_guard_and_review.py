from __future__ import annotations

import pytest
from sqlalchemy import select

from dispute_agent.agents.question_planner import EvidenceGapQuestionPlanner
from dispute_agent.agents.runtime import AgentRuntime
from dispute_agent.agents.workflow import InvestigationWorkflow
from dispute_agent.errors import ValidationError
from dispute_agent.models import Approval, Decision, DecisionGuardReport, ResolutionAction
from dispute_agent.serialization import content_hash
from dispute_agent.services.decision_guard import DecisionGuard
from dispute_agent.services.human_review import HumanReviewService


def services(context):  # type: ignore[no-untyped-def]
    runtime = AgentRuntime(context.sessions, tools=context.tools)
    guard = DecisionGuard(context.sessions)
    workflow = InvestigationWorkflow(
        context.sessions,
        orchestrator=context.orchestrator,
        runtime=runtime,
        tools=context.tools,
        decision_guard=guard,
    )
    review = HumanReviewService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
    )
    return runtime, guard, workflow, review


def reach_guard_without_running_guard(context, case_id: str) -> tuple[AgentRuntime, dict]:  # type: ignore[no-untyped-def]
    runtime = AgentRuntime(context.sessions, tools=context.tools)
    view = context.orchestrator.start(case_id)
    buyer, seller = runtime.run_party_analysts(case_id, view["case_run_id"])
    view = context.orchestrator.submit_phase_result(
        case_id,
        case_run_id=view["case_run_id"],
        phase="PARTY_ANALYSIS",
        payload={"buyer_analysis": {"output_id": buyer.output_id}, "seller_analysis": {"output_id": seller.output_id}},
        actor="test",
    )
    report = runtime.run_evidence_clerk(case_id, view["case_run_id"])
    view = context.orchestrator.submit_phase_result(
        case_id,
        case_run_id=view["case_run_id"],
        phase="EVIDENCE_REVIEW",
        payload={"evidence_policy_report": {"output_id": report.output_id}},
        actor="test",
    )
    plan = EvidenceGapQuestionPlanner(context.sessions, tools=context.tools).plan(case_id, view["case_run_id"])
    assert plan["question_ids"] == []
    view = context.orchestrator.submit_phase_result(
        case_id,
        case_run_id=view["case_run_id"],
        phase="GAP_RESOLUTION",
        payload=plan,
        actor="test",
    )
    recommendation = runtime.run_adjudicator(case_id, view["case_run_id"])
    payload = recommendation.payload
    draft = context.tools.call(
        "resolution.create_draft",
        {
            "case_id": case_id,
            "source_agent_output_id": recommendation.output_id,
            "outcome": payload["outcome"],
            "refund_amount_minor": payload.get("refund_amount_minor"),
            "shipping_payer": payload["shipping_payer"],
            "actions": payload["proposed_actions"],
            "payload": payload,
        },
        actor="ADJUDICATION_AGENT",
        case_run_id=view["case_run_id"],
    )
    view = context.orchestrator.submit_phase_result(
        case_id,
        case_run_id=view["case_run_id"],
        phase="ADJUDICATION",
        payload={"decision_id": draft["decision_id"]},
        actor="test",
    )
    assert view["phase"] == "GUARD_CHECK"
    return runtime, view


def test_guard_passes_valid_draft_with_risk_warning_and_freezes_review_package(context) -> None:
    _runtime, guard, workflow, review = services(context)
    result = workflow.run_until_blocked("case_clear_mismatch")
    assert result["state"] == "HUMAN_REVIEW"

    guard_results = guard.list_for_case("case_clear_mismatch")
    assert len(guard_results) == 1
    assert guard_results[0].passed is True
    assert guard_results[0].required_action == "PROCEED_TO_HUMAN_REVIEW"
    assert any(item.code == "SUGGESTION_LIMIT_EXCEEDED" and item.severity == "WARN" for item in guard_results[0].violations)
    replay = guard.evaluate(
        "case_clear_mismatch",
        result["case_run_id"],
        decision_id=result["checkpoint"]["decision_id"],
    )
    assert replay.guard_result_id == guard_results[0].guard_result_id

    package = review.get_review_package("case_clear_mismatch")
    assert package["package_integrity_valid"] is True
    assert package["review_package_hash"] == result["checkpoint"]["review_package_hash"]
    assert package["decision"]["content_sha256"] == guard_results[0].decision_content_sha256


def test_guard_blocks_tampered_draft_and_returns_to_adjudication(context) -> None:
    _runtime, view = reach_guard_without_running_guard(context, "case_clear_mismatch")
    with context.sessions() as session:
        decision = session.get(Decision, view["checkpoint"]["decision_id"])
        assert decision is not None
        tampered = {**decision.payload_json, "claim_findings": []}
        decision.payload_json = tampered
        decision.content_sha256 = content_hash(tampered)
        session.commit()

    result = DecisionGuard(context.sessions).evaluate(
        "case_clear_mismatch",
        view["case_run_id"],
        decision_id=view["checkpoint"]["decision_id"],
    )
    assert result.passed is False
    codes = {item.code for item in result.violations if item.severity == "BLOCK"}
    assert "SOURCE_PAYLOAD_MISMATCH" in codes
    assert "MATERIAL_CLAIM_OMITTED" in codes

    returned = context.orchestrator.submit_phase_result(
        "case_clear_mismatch",
        case_run_id=view["case_run_id"],
        phase="GUARD_CHECK",
        payload=result.model_dump(mode="json"),
        actor="test",
    )
    assert returned["state"] == "UNDER_INVESTIGATION"
    assert returned["phase"] == "ADJUDICATION"


def test_authorized_reviewer_approves_fixed_decision_and_actions(context) -> None:
    _runtime, _guard, workflow, review = services(context)
    workflow.run_until_blocked("case_clear_mismatch")
    package = review.get_review_package("case_clear_mismatch")

    with pytest.raises(ValidationError):
        review.approve(
            "case_clear_mismatch",
            decision_id=package["decision"]["decision_id"],
            reviewer_id="user_buyer_demo",
            reason="买方不能充当审核员。",
        )

    approved = review.approve(
        "case_clear_mismatch",
        decision_id=package["decision"]["decision_id"],
        reviewer_id="user_reviewer_demo",
        reason="证据、政策版本和金额均已复核，批准该固定版本。",
    )
    assert approved["state"] == "APPROVED"
    assert approved["decision_status"] == "APPROVED"
    assert {item["status"] for item in approved["resolution_actions"]} == {"APPROVED"}

    replay = review.approve(
        "case_clear_mismatch",
        decision_id=package["decision"]["decision_id"],
        reviewer_id="user_reviewer_demo",
        reason="重复提交不应产生第二条批准。",
    )
    assert replay["review_record"]["approval_id"] == approved["review_record"]["approval_id"]
    with context.sessions() as session:
        assert len(list(session.scalars(select(Approval).where(Approval.decision_id == package["decision"]["decision_id"])))) == 1


def test_reviewer_can_reject_draft_and_cancel_execution_plan(context) -> None:
    _runtime, _guard, workflow, review = services(context)
    workflow.run_until_blocked("case_clear_mismatch")
    package = review.get_review_package("case_clear_mismatch")
    rejected = review.reject(
        "case_clear_mismatch",
        decision_id=package["decision"]["decision_id"],
        reviewer_id="user_reviewer_demo",
        reason="检测材料的设备关联仍需线下复核，不接受当前草稿。",
    )
    assert rejected["state"] == "REJECTED"
    assert rejected["decision_status"] == "REJECTED"
    assert {item["status"] for item in rejected["resolution_actions"]} == {"CANCELLED"}


def test_reviewer_can_return_selected_claims_to_a_new_investigation_run(context) -> None:
    _runtime, _guard, workflow, review = services(context)
    first = workflow.run_until_blocked("case_clear_mismatch")
    package = review.get_review_package("case_clear_mismatch")
    returned = review.return_to_investigation(
        "case_clear_mismatch",
        decision_id=package["decision"]["decision_id"],
        reviewer_id="user_reviewer_demo",
        instructions="重新检查第三方检测报告的设备序列号与形成时间。",
        claim_ids=["claim_clear_mismatch_buyer"],
    )
    assert returned["state"] == "UNDER_INVESTIGATION"
    assert returned["case_run_id"] != first["case_run_id"]
    assert returned["run_number"] == 2
    assert returned["checkpoint"]["dirty_claim_ids"] == ["claim_clear_mismatch_buyer"]
    with context.sessions() as session:
        decision = session.get(Decision, package["decision"]["decision_id"])
        actions = list(session.scalars(select(ResolutionAction).where(ResolutionAction.decision_id == decision.id)))
        assert decision is not None and decision.status == "REVISION_REQUESTED"
        assert {item.status for item in actions} == {"CANCELLED"}
    preserved_package = review.get_review_package(
        "case_clear_mismatch",
        decision_id=package["decision"]["decision_id"],
    )
    assert preserved_package["package_integrity_valid"] is True
