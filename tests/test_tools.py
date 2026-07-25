from __future__ import annotations

import pytest
from sqlalchemy import func, select

from dispute_agent.errors import AuthorizationError
from dispute_agent.models import ToolCall


def test_read_tools_use_pinned_policy_and_write_audit(context) -> None:
    run = context.orchestrator.start("case_clear_mismatch")
    transaction = context.tools.call(
        "transaction.get",
        {"case_id": "case_clear_mismatch"},
        actor="REVIEWER",
        case_run_id=run["case_run_id"],
    )
    assert transaction["paid_amount"] == {"currency": "CNY", "amount_minor": 320000}
    policy = context.tools.call(
        "policy.search",
        {"case_id": "case_clear_mismatch", "query": "描述不符"},
        actor="EVIDENCE_POLICY_CLERK",
        case_run_id=run["case_run_id"],
    )
    assert policy["version"] == "2.0.0"
    assert all(item["citation_key"].startswith("marketplace.description_mismatch@2.0.0#") for item in policy["rules"])
    with context.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ToolCall).where(ToolCall.status == "SUCCEEDED")) == 2


def test_question_is_claim_linked_and_deduplicated(context) -> None:
    run = context.orchestrator.start("case_buyer_missing_evidence")
    params = {
        "case_id": "case_buyer_missing_evidence",
        "target": "BUYER",
        "question": "请提交同时显示设备序列号和内存容量的检测报告。",
        "missing_fact": "收到设备的实际内存和设备身份",
        "resolves_claim_ids": ["claim_buyer_missing_evidence_buyer"],
        "acceptable_evidence_types": ["DEVICE_REPORT"],
    }
    result = context.tools.call(
        "case.add_open_question",
        params,
        actor="EVIDENCE_POLICY_CLERK",
        case_run_id=run["case_run_id"],
    )
    assert result["round_number"] == 1
    replay = context.tools.call(
        "case.add_open_question",
        {**params, "question": "请补充可同时证明序列号与内存配置的原始检测材料。"},
        actor="EVIDENCE_POLICY_CLERK",
        case_run_id=run["case_run_id"],
    )
    assert replay["question_id"] == result["question_id"]
    assert replay["reused"] is True
    assert replay["created"] is False
    with context.sessions() as session:
        statuses = list(session.scalars(select(ToolCall.status).order_by(ToolCall.called_at)))
        assert statuses[-2:] == ["SUCCEEDED", "SUCCEEDED"]


def test_evidence_is_case_scoped_and_mock_execution_is_not_bypassable(context) -> None:
    with pytest.raises(AuthorizationError):
        context.tools.call(
            "evidence.inspect",
            {"case_id": "case_clear_mismatch", "evidence_id": "ev_serial_conflict_buyer_report"},
            actor="EVIDENCE_POLICY_CLERK",
        )
    with pytest.raises(AuthorizationError):
        context.tools.call(
            "resolution.execute_mock",
            {"action_id": "does_not_matter", "idempotency_key": "wrong"},
            actor="ADJUDICATION_AGENT",
        )
