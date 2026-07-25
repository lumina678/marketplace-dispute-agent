from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import func, select

from dispute_agent.agents.heuristics import build_decision_recommendation
from dispute_agent.agents.runtime import AgentRuntime
from dispute_agent.agents.workflow import InvestigationWorkflow
from dispute_agent.models import AgentOutput, Decision, OpenQuestion, ResolutionAction
from dispute_agent.services.evidence_submission import EvidenceSubmissionService


def make_workflow(context) -> tuple[AgentRuntime, InvestigationWorkflow]:  # type: ignore[no-untyped-def]
    runtime = AgentRuntime(context.sessions, tools=context.tools)
    workflow = InvestigationWorkflow(
        context.sessions,
        orchestrator=context.orchestrator,
        runtime=runtime,
        tools=context.tools,
    )
    return runtime, workflow


def test_clear_mismatch_runs_independent_roles_and_reaches_human_review(context) -> None:
    runtime, workflow = make_workflow(context)
    result = workflow.run_until_blocked("case_clear_mismatch")

    assert result["state"] == "HUMAN_REVIEW"
    assert result["phase"] == "HUMAN_REVIEW"
    assert result["workflow_boundary"] == "HUMAN_REVIEW_REQUIRED"
    outputs = runtime.list_outputs("case_clear_mismatch", result["case_run_id"])
    assert {item.role for item in outputs} == {
        "BUYER_CASE_ANALYST",
        "SELLER_CASE_ANALYST",
        "EVIDENCE_POLICY_CLERK",
        "ADJUDICATION_AGENT",
    }
    assert len({item.output_id for item in outputs}) == 4

    clerk = next(item for item in outputs if item.role == "EVIDENCE_POLICY_CLERK")
    assert clerk.payload["policy_version"] == "2.0.0"
    assert clerk.payload["policy_citations"]
    assert all(item["policy_version"] == "2.0.0" for item in clerk.payload["policy_citations"])

    with context.sessions() as session:
        decision = session.get(Decision, result["checkpoint"]["decision_id"])
        assert decision is not None
        assert decision.outcome == "RETURN_AND_FULL_REFUND"
        assert decision.payload_json["refund_amount"] == {"currency": "CNY", "amount_minor": 320000}
        assert decision.payload_json["shipping_payer"] == "SELLER"
        assert decision.agent_output_id is not None
        actions = list(
            session.scalars(select(ResolutionAction).where(ResolutionAction.decision_id == decision.id))
        )
        assert {(item.action_type, item.amount_minor) for item in actions} == {
            ("CREATE_RETURN", None),
            ("FULL_REFUND", 320000),
        }

    replay = workflow.run_until_blocked("case_clear_mismatch")
    assert replay["checkpoint"]["decision_id"] == result["checkpoint"]["decision_id"]
    with context.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AgentOutput)) == 4
        assert session.scalar(
            select(func.count()).select_from(Decision).where(Decision.dispute_id == "case_clear_mismatch")
        ) == 1


def test_missing_evidence_pauses_then_submission_resumes_to_new_draft(context) -> None:
    _, workflow = make_workflow(context)
    waiting = workflow.run_until_blocked("case_buyer_missing_evidence")
    assert waiting["state"] == "WAITING_FOR_BUYER"
    assert waiting["workflow_boundary"] == "EVIDENCE_RESPONSE_REQUIRED"
    question_ids = waiting["checkpoint"]["question_ids"]
    old_run_id = waiting["case_run_id"]

    submission = EvidenceSubmissionService(
        context.sessions,
        orchestrator=context.orchestrator,
    ).submit_and_resume(
        "case_buyer_missing_evidence",
        target="BUYER",
        question_ids=question_ids,
        evidence_type="DEVICE_REPORT",
        description="第三方原报告显示涉案设备为 8GB，序列号 SN-MISSING-001。",
        source_record_id="buyer-supplement-001",
        captured_at=datetime.now(timezone.utc),
        content_sha256=sha256(b"buyer-supplement-001").hexdigest(),
        extracted_facts=[
            {"field": "detected_memory_gb", "value": 8},
            {"field": "detected_serial", "value": "SN-MISSING-001"},
            {"field": "quality", "value": "THIRD_PARTY"},
        ],
    )
    assert submission["run"]["case_run_id"] != old_run_id
    assert submission["run"]["checkpoint"]["dirty_claim_ids"] == [
        "claim_buyer_missing_evidence_buyer"
    ]

    result = workflow.run_until_blocked("case_buyer_missing_evidence")
    assert result["phase"] == "HUMAN_REVIEW"
    with context.sessions() as session:
        question = session.get(OpenQuestion, question_ids[0])
        decision = session.get(Decision, result["checkpoint"]["decision_id"])
        assert question is not None and question.status == "ANSWERED"
        assert question.response_evidence_ids_json == [submission["evidence"]["evidence_id"]]
        assert decision is not None and decision.outcome == "RETURN_AND_FULL_REFUND"
        assert decision.version == 1


def test_serial_conflict_never_converts_swap_suspicion_into_fact(context) -> None:
    _, workflow = make_workflow(context)
    waiting = workflow.run_until_blocked("case_serial_conflict")
    assert waiting["state"] == "WAITING_FOR_SELLER"

    EvidenceSubmissionService(context.sessions, orchestrator=context.orchestrator).submit_and_resume(
        "case_serial_conflict",
        target="SELLER",
        question_ids=waiting["checkpoint"]["question_ids"],
        evidence_type="VIDEO",
        description="卖方发货前原始视频显示 16GB 和序列号 SN-ORIGINAL-001。",
        source_record_id="seller-preshipment-001",
        captured_at=datetime.now(timezone.utc),
        content_sha256=sha256(b"seller-preshipment-001").hexdigest(),
        extracted_facts=[
            {"field": "preshipment_memory_gb", "value": 16},
            {"field": "preshipment_serial", "value": "SN-ORIGINAL-001"},
        ],
    )
    result = workflow.run_until_blocked("case_serial_conflict")
    assert result["state"] == "HUMAN_REVIEW"
    with context.sessions() as session:
        decision = session.get(Decision, result["checkpoint"]["decision_id"])
        assert decision is not None and decision.outcome == "ESCALATE_TO_HUMAN"
        seller_finding = next(
            item for item in decision.payload_json["claim_findings"]
            if item["claim_id"] == "claim_serial_conflict_seller"
        )
        assert seller_finding["finding"] == "INSUFFICIENT_EVIDENCE"
        assert "不能单独证明买方更换硬件" in seller_finding["rationale"]
        assert all("买方实施调包" not in item["statement"] for item in decision.payload_json["established_facts"])


def test_decision_draft_is_idempotent_for_one_adjudication_output(context) -> None:
    runtime, workflow = make_workflow(context)
    result = workflow.run_until_blocked("case_clear_mismatch")
    output = next(
        item for item in runtime.list_outputs("case_clear_mismatch", result["case_run_id"])
        if item.role == "ADJUDICATION_AGENT"
    )
    payload = output.payload
    replay = context.tools.call(
        "resolution.create_draft",
        {
            "case_id": "case_clear_mismatch",
            "source_agent_output_id": output.output_id,
            "outcome": payload["outcome"],
            "refund_amount_minor": payload["refund_amount_minor"],
            "shipping_payer": payload["shipping_payer"],
            "actions": payload["proposed_actions"],
            "payload": payload,
        },
        actor="ADJUDICATION_AGENT",
        case_run_id=result["case_run_id"],
    )
    assert replay["decision_id"] == result["checkpoint"]["decision_id"]
    assert replay["idempotent_replay"] is True


def test_partial_reevaluation_preserves_unaffected_prior_finding() -> None:
    previous_seller_finding = {
        "claim_id": "claim_seller",
        "finding": "NOT_SUPPORTED",
        "rationale": "上一版本已经审查该抗辩。",
        "evidence_ids": ["ev_seller"],
        "policy_citation_ids": ["old_citation"],
    }
    result = build_decision_recommendation(
        {
            "case_id": "case_partial",
            "case_run_id": "run_2",
            "analyzed_claim_ids": ["claim_buyer"],
            "claims": [
                {"claim_id": "claim_buyer", "party": "BUYER", "claim_type": "CONFIG_MISMATCH"},
                {"claim_id": "claim_seller", "party": "SELLER", "claim_type": "CONFIG_MISMATCH"},
            ],
            "evidence": [
                {
                    "evidence_id": "ev_listing",
                    "extracted_facts": [
                        {"field": "promised_memory_gb", "value": 16},
                        {"field": "listing_serial", "value": "SN-1"},
                    ],
                },
                {
                    "evidence_id": "ev_buyer",
                    "extracted_facts": [
                        {"field": "detected_memory_gb", "value": 8},
                        {"field": "detected_serial", "value": "SN-1"},
                    ],
                },
            ],
            "evidence_report": {
                "policy_citations": [
                    {"citation_id": "new_definition", "rule_id": "DM-DEF-01"},
                    {"citation_id": "new_burden", "rule_id": "DM-BURDEN-01"},
                    {"citation_id": "new_remedy", "rule_id": "DM-REMEDY-01"},
                ],
                "requires_human_review": False,
                "human_review_reasons": [],
            },
            "transaction": {"paid_amount": {"currency": "CNY", "amount_minor": 100000}},
            "open_questions": [],
            "previous_decision": {
                "claim_findings": [previous_seller_finding],
                "established_facts": [],
            },
        }
    )
    findings = {item.claim_id: item for item in result.claim_findings}
    assert findings["claim_buyer"].finding == "SUPPORTED"
    assert findings["claim_seller"].model_dump(mode="json") == previous_seller_finding
    assert "old_citation" in result.policy_citation_ids
