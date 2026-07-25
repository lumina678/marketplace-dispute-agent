from __future__ import annotations

from datetime import timedelta
from hashlib import sha256

from sqlalchemy import select

from dispute_agent.models import CaseRun, Dispute, Evidence, OpenQuestion, utc_now


def submit_to_gap(context, case_id: str) -> dict:
    run = context.orchestrator.start(case_id)
    run = context.orchestrator.submit_phase_result(
        case_id,
        case_run_id=run["case_run_id"],
        phase="PARTY_ANALYSIS",
        payload={"buyer_analysis": {"claims": []}, "seller_analysis": {"claims": []}},
        actor="test",
        tokens_used=100,
    )
    run = context.orchestrator.submit_phase_result(
        case_id,
        case_run_id=run["case_run_id"],
        phase="EVIDENCE_REVIEW",
        payload={"evidence_assessments": [], "policy_citations": []},
        actor="test",
        tokens_used=100,
    )
    assert run["phase"] == "GAP_RESOLUTION"
    return run


def test_full_harness_reaches_human_review_after_guard(context) -> None:
    run = submit_to_gap(context, "case_clear_mismatch")
    run = context.orchestrator.submit_phase_result(
        "case_clear_mismatch",
        case_run_id=run["case_run_id"],
        phase="GAP_RESOLUTION",
        payload={"question_ids": []},
        actor="test",
    )
    draft = context.tools.call(
        "resolution.create_draft",
        {
            "case_id": "case_clear_mismatch",
            "outcome": "RETURN_AND_FULL_REFUND",
            "refund_amount_minor": 320000,
            "shipping_payer": "SELLER",
            "payload": {"claim_findings": [{"claim_id": "claim_clear_mismatch_buyer", "finding": "SUPPORTED"}]},
            "actions": [{"action_type": "FULL_REFUND", "amount_minor": 320000}],
        },
        actor="TEST",
        case_run_id=run["case_run_id"],
    )
    run = context.orchestrator.submit_phase_result(
        "case_clear_mismatch",
        case_run_id=run["case_run_id"],
        phase="ADJUDICATION",
        payload={"decision_id": draft["decision_id"]},
        actor="test",
        tokens_used=200,
    )
    run = context.orchestrator.submit_phase_result(
        "case_clear_mismatch",
        case_run_id=run["case_run_id"],
        phase="GUARD_CHECK",
        payload={"passed": True, "violations": []},
        actor="test",
    )
    assert run["state"] == "HUMAN_REVIEW"
    assert run["run_status"] == "COMPLETED"
    assert run["phase"] == "HUMAN_REVIEW"
    replay = context.orchestrator.replay("case_clear_mismatch")
    assert replay["consistent"] is True
    assert replay["event_count"] == 4


def test_external_evidence_pauses_and_creates_a_new_run_on_resume(context) -> None:
    run = submit_to_gap(context, "case_buyer_missing_evidence")
    question = context.tools.call(
        "case.add_open_question",
        {
            "case_id": "case_buyer_missing_evidence",
            "target": "BUYER",
            "question": "请提交同时显示设备序列号和内存容量的检测报告。",
            "missing_fact": "收到设备的实际内存和设备身份",
            "resolves_claim_ids": ["claim_buyer_missing_evidence_buyer"],
            "acceptable_evidence_types": ["DEVICE_REPORT"],
        },
        actor="EVIDENCE_POLICY_CLERK",
        case_run_id=run["case_run_id"],
    )
    waiting = context.orchestrator.submit_phase_result(
        "case_buyer_missing_evidence",
        case_run_id=run["case_run_id"],
        phase="GAP_RESOLUTION",
        payload={"question_ids": [question["question_id"]]},
        actor="test",
    )
    assert waiting["state"] == "WAITING_FOR_BUYER"
    old_run_id = waiting["case_run_id"]

    description = "补充的第三方报告显示 8GB，序列号 SN-MISSING-001。"
    evidence = Evidence(
        id="ev_buyer_missing_evidence_supplement",
        dispute_id="case_buyer_missing_evidence",
        submitted_by="BUYER",
        evidence_type="DEVICE_REPORT",
        description=description,
        source_system="EVIDENCE_STORE",
        source_record_id="supplement_001",
        captured_at=utc_now(),
        submitted_at=utc_now(),
        content_sha256=sha256(description.encode()).hexdigest(),
        immutable_uri="evidence://case_buyer_missing_evidence/supplement_001",
        integrity_status="HASH_VERIFIED",
        related_claim_ids_json=["claim_buyer_missing_evidence_buyer"],
        extracted_facts_json=[{"field": "detected_memory_gb", "value": 8}],
        handling_flags_json=[],
    )
    with context.sessions() as session:
        session.add(evidence)
        session.commit()
    resumed = context.orchestrator.resume_with_evidence(
        "case_buyer_missing_evidence",
        target="BUYER",
        evidence_id=evidence.id,
        question_ids=[question["question_id"]],
    )
    assert resumed["state"] == "UNDER_INVESTIGATION"
    assert resumed["case_run_id"] != old_run_id
    assert resumed["run_number"] == 2
    assert resumed["checkpoint"]["dirty_claim_ids"] == ["claim_buyer_missing_evidence_buyer"]
    with context.sessions() as session:
        old_run = session.get(CaseRun, old_run_id)
        stored_question = session.get(OpenQuestion, question["question_id"])
        assert old_run.status == "COMPLETED"
        assert stored_question.status == "ANSWERED"
    assert context.orchestrator.replay("case_buyer_missing_evidence")["consistent"] is True


def test_budget_exhaustion_escalates_without_losing_checkpoint(context) -> None:
    run = context.orchestrator.start("case_seller_preshipment")
    with context.sessions() as session:
        stored = session.get(CaseRun, run["case_run_id"])
        stored.token_budget = 10
        session.commit()
    escalated = context.orchestrator.submit_phase_result(
        "case_seller_preshipment",
        case_run_id=run["case_run_id"],
        phase="PARTY_ANALYSIS",
        payload={"buyer_analysis": {}, "seller_analysis": {}},
        actor="test",
        tokens_used=11,
    )
    assert escalated["state"] == "HUMAN_REVIEW"
    assert escalated["run_status"] == "ESCALATED"
    assert escalated["checkpoint"]["pause_reason"] == "BUDGET_EXCEEDED"


def test_recover_reuses_checkpoint_instead_of_rerunning(context) -> None:
    run = context.orchestrator.start("case_serial_conflict")
    original_checkpoint = run["checkpoint"]
    with context.sessions() as session:
        stored = session.get(CaseRun, run["case_run_id"])
        stored.status = "RUNNING"
        stored.paused_at = None
        session.commit()
    recovered = context.orchestrator.recover("case_serial_conflict")
    assert recovered["case_run_id"] == run["case_run_id"]
    assert recovered["resumed"] is True
    assert recovered["run_status"] == "PAUSED"
    assert recovered["checkpoint"]["completed_phases"] == original_checkpoint["completed_phases"]
    assert recovered["checkpoint"]["pause_reason"] == "RECOVERED_AFTER_PROCESS_RESTART"
