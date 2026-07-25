from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest
from sqlalchemy import func, select

from dispute_agent.agents.runtime import AgentRuntime
from dispute_agent.agents.workflow import InvestigationWorkflow
from dispute_agent.errors import AuthorizationError, ConflictError, ValidationError
from dispute_agent.models import Appeal, CaseEvent, CaseRun, Decision, ResolutionAction, ToolCall, Transaction
from dispute_agent.services.appeals import AppealService
from dispute_agent.services.evidence_submission import EvidenceSubmissionService
from dispute_agent.services.execution import ResolutionExecutionService
from dispute_agent.services.human_review import HumanReviewService
from dispute_agent.services.tools import ToolService


def make_services(context):  # type: ignore[no-untyped-def]
    runtime = AgentRuntime(context.sessions, tools=context.tools)
    workflow = InvestigationWorkflow(
        context.sessions,
        orchestrator=context.orchestrator,
        runtime=runtime,
        tools=context.tools,
    )
    review = HumanReviewService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
    )
    execution = ResolutionExecutionService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
        tools=context.tools,
    )
    appeals = AppealService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
    )
    return workflow, review, execution, appeals


def approve_case(context, case_id: str = "case_clear_mismatch") -> str:  # type: ignore[no-untyped-def]
    workflow, review, _execution, _appeals = make_services(context)
    result = workflow.run_until_blocked(case_id)
    assert result["state"] == "HUMAN_REVIEW"
    package = review.get_review_package(case_id)
    review.approve(
        case_id,
        decision_id=package["decision"]["decision_id"],
        reviewer_id="user_reviewer_demo",
        reason="测试中复核固定决定、证据、规则和金额后批准。",
    )
    return package["decision"]["decision_id"]


def resolve_clear_case(context, *, paid_at: datetime | None = None) -> tuple[str, dict]:  # type: ignore[no-untyped-def]
    if paid_at is not None:
        with context.sessions() as session:
            transaction = session.get(Transaction, "txn_clear_mismatch")
            assert transaction is not None
            transaction.paid_at = paid_at
            session.commit()
    decision_id = approve_case(context)
    execution = ResolutionExecutionService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
        tools=context.tools,
    )
    return decision_id, execution.execute("case_clear_mismatch", decision_id=decision_id)


def appeal_evidence(source_record_id: str) -> dict:
    return {
        "evidence_type": "DEVICE_REPORT",
        "description": "申诉阶段提交的第三方原始检测报告，显示 8GB 和涉案设备序列号。",
        "source_record_id": source_record_id,
        "captured_at": datetime.now(timezone.utc),
        "related_claim_ids": ["claim_clear_mismatch_buyer"],
        "extracted_facts": [
            {"field": "detected_memory_gb", "value": 8},
            {"field": "detected_serial", "value": "SN-CLEAR-001"},
            {"field": "quality", "value": "THIRD_PARTY"},
        ],
        "content_sha256": sha256(source_record_id.encode()).hexdigest(),
    }


def test_approved_full_refund_executes_once_and_replays_idempotently(context) -> None:
    decision_id = approve_case(context)
    execution = ResolutionExecutionService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
        tools=context.tools,
    )

    first = execution.execute("case_clear_mismatch", decision_id=decision_id)
    assert first["state"] == "RESOLVED"
    assert first["readback"]["verified"] is True
    assert first["transaction"] == {
        "transaction_id": "txn_clear_mismatch",
        "order_status": "REFUND_COMPLETED",
        "funds_status": "REFUNDED",
    }
    assert {(item["action_type"], item["status"]) for item in first["actions"]} == {
        ("CREATE_RETURN", "SUCCEEDED"),
        ("FULL_REFUND", "SUCCEEDED"),
    }
    original_references = {item["action_id"]: item["external_reference"] for item in first["actions"]}
    original_results = {item["action_id"]: item["result"] for item in first["actions"]}

    replay = execution.execute("case_clear_mismatch", decision_id=decision_id)
    assert replay["state"] == "RESOLVED"
    assert replay["idempotent_replay"] is True
    assert {item["action_id"]: item["external_reference"] for item in replay["actions"]} == original_references
    assert {item["action_id"]: item["result"] for item in replay["actions"]} == original_results
    with context.sessions() as session:
        execution_calls = list(
            session.scalars(
                select(ToolCall)
                .where(ToolCall.tool_name == "resolution.execute_mock")
                .order_by(ToolCall.called_at)
            )
        )
    assert execution_calls
    assert all(item.dispute_id == "case_clear_mismatch" for item in execution_calls)


def test_mock_execution_is_bound_to_the_action_case(context) -> None:
    decision_id = approve_case(context)
    with context.sessions() as session:
        action = session.scalar(
            select(ResolutionAction)
            .where(ResolutionAction.decision_id == decision_id)
            .order_by(ResolutionAction.created_at)
            .limit(1)
        )
        assert action is not None
        action_id = action.id
        idempotency_key = action.idempotency_key

    with pytest.raises(AuthorizationError, match="不属于指定案件"):
        context.tools.call(
            "resolution.execute_mock",
            {
                "case_id": "case_serial_conflict",
                "action_id": action_id,
                "idempotency_key": idempotency_key,
            },
            actor="EXECUTOR",
        )


def test_no_action_decision_resolves_without_touching_mock_ledger(context) -> None:
    workflow, review, execution, _appeals = make_services(context)
    waiting = workflow.run_until_blocked("case_serial_conflict")
    EvidenceSubmissionService(
        context.sessions,
        orchestrator=context.orchestrator,
    ).submit_and_resume(
        "case_serial_conflict",
        target="SELLER",
        question_ids=waiting["checkpoint"]["question_ids"],
        evidence_type="VIDEO",
        description="卖方发货前原始视频显示 16GB 和序列号 SN-ORIGINAL-001。",
        source_record_id="seller-preshipment-execution-test",
        captured_at=datetime.now(timezone.utc),
        content_sha256=sha256(b"seller-preshipment-execution-test").hexdigest(),
        extracted_facts=[
            {"field": "preshipment_memory_gb", "value": 16},
            {"field": "preshipment_serial", "value": "SN-ORIGINAL-001"},
        ],
    )
    result = workflow.run_until_blocked("case_serial_conflict")
    package = review.get_review_package("case_serial_conflict")
    assert package["resolution_actions"] == []
    review.approve(
        "case_serial_conflict",
        decision_id=package["decision"]["decision_id"],
        reviewer_id="user_reviewer_demo",
        reason="序列号冲突维持人工升级建议，批准无资金动作决定。",
    )

    executed = execution.execute(
        "case_serial_conflict",
        decision_id=package["decision"]["decision_id"],
    )
    assert result["state"] == "HUMAN_REVIEW"
    assert executed["state"] == "RESOLVED"
    assert executed["actions"] == []
    assert executed["transaction"]["order_status"] == "DISPUTED"
    assert executed["transaction"]["funds_status"] == "HELD"


class FailSecondActionOnceToolService(ToolService):
    def __init__(self, session_factory):  # type: ignore[no-untyped-def]
        super().__init__(session_factory)
        self.failed = False

    def call(self, tool_name, parameters, *, actor, case_run_id=None):  # type: ignore[no-untyped-def]
        if tool_name == "resolution.execute_mock" and not self.failed:
            with self.session_factory() as session:
                action = session.get(ResolutionAction, parameters["action_id"])
                if action is not None and action.action_type == "FULL_REFUND":
                    self.failed = True
                    raise RuntimeError("injected second action failure")
        return super().call(tool_name, parameters, actor=actor, case_run_id=case_run_id)


def test_failed_second_action_can_retry_without_repeating_successful_action(context) -> None:
    decision_id = approve_case(context)
    faulting_tools = FailSecondActionOnceToolService(context.sessions)
    execution = ResolutionExecutionService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
        tools=faulting_tools,
    )

    failed = execution.execute("case_clear_mismatch", decision_id=decision_id)
    assert failed["state"] == "EXECUTION_FAILED"
    actions = {item["action_type"]: item for item in failed["actions"]}
    assert actions["CREATE_RETURN"]["status"] == "SUCCEEDED"
    assert actions["FULL_REFUND"]["status"] == "FAILED"
    return_reference = actions["CREATE_RETURN"]["external_reference"]
    return_result = actions["CREATE_RETURN"]["result"]

    retried = execution.retry("case_clear_mismatch", decision_id=decision_id)
    assert retried["state"] == "RESOLVED"
    assert retried["readback"]["verified"] is True
    retried_actions = {item["action_type"]: item for item in retried["actions"]}
    assert retried_actions["CREATE_RETURN"]["external_reference"] == return_reference
    assert retried_actions["CREATE_RETURN"]["result"] == return_result
    assert retried_actions["FULL_REFUND"]["status"] == "SUCCEEDED"
    assert retried["transaction"]["funds_status"] == "REFUNDED"


def test_appeal_validates_party_identity_and_requires_evidence_for_new_evidence(context) -> None:
    resolve_clear_case(context)
    appeals = AppealService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
    )
    with pytest.raises(ValidationError, match="身份"):
        appeals.submit(
            "case_clear_mismatch",
            appellant_id="user_seller_demo",
            appellant_role="BUYER",
            grounds="OTHER",
            statement="身份不匹配的申诉。",
        )
    with pytest.raises(ValidationError, match="至少一项证据"):
        appeals.submit(
            "case_clear_mismatch",
            appellant_id="user_buyer_demo",
            appellant_role="BUYER",
            grounds="NEW_EVIDENCE",
            statement="声称有新证据但没有提交任何证据。",
        )


@pytest.mark.parametrize(
    ("paid_at", "expected_version", "expected_hours"),
    [
        (datetime(2025, 7, 1, tzinfo=timezone.utc), "1.0.0", 72),
        (datetime(2026, 7, 1, tzinfo=timezone.utc), "2.0.0", 120),
    ],
)
def test_appeal_deadline_is_pinned_to_transaction_policy_version(
    context,
    paid_at: datetime,
    expected_version: str,
    expected_hours: int,
) -> None:
    _decision_id, resolved = resolve_clear_case(context, paid_at=paid_at)
    with context.sessions() as session:
        resolved_event = session.scalar(
            select(CaseEvent)
            .where(CaseEvent.dispute_id == "case_clear_mismatch", CaseEvent.to_state == "RESOLVED")
            .order_by(CaseEvent.sequence.desc())
            .limit(1)
        )
        assert resolved_event is not None
        submitted_at = resolved_event.occurred_at + timedelta(hours=1)

    appeals = AppealService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
    )
    submitted = appeals.submit(
        "case_clear_mismatch",
        appellant_id="user_buyer_demo",
        appellant_role="BUYER",
        grounds="OTHER",
        statement="在申诉期内请求复核原决定。",
        now=submitted_at,
    )
    assert resolved["state"] == "RESOLVED"
    assert submitted["state"] == "APPEALED"
    assert submitted["policy"]["version"] == expected_version
    assert datetime.fromisoformat(submitted["deadline"]) - resolved_event.occurred_at == timedelta(
        hours=expected_hours
    )


def test_new_evidence_appeal_reopens_into_a_new_case_run(context) -> None:
    resolve_clear_case(context)
    appeals = AppealService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
    )
    submitted = appeals.submit(
        "case_clear_mismatch",
        appellant_id="user_buyer_demo",
        appellant_role="BUYER",
        grounds="NEW_EVIDENCE",
        statement="补交第三方原始报告，请重新检查决定依据。",
        new_evidence=[appeal_evidence("appeal-new-report-001")],
    )
    assert submitted["state"] == "APPEALED"
    assert len(submitted["evidence_ids"]) == 1

    accepted = appeals.accept(
        "case_clear_mismatch",
        appeal_id=submitted["appeal_id"],
        reviewer_id="user_reviewer_demo",
        reason="确认存在原决定未引用的新增第三方报告。",
    )
    assert accepted["state"] == "UNDER_INVESTIGATION"
    assert accepted["run_number"] == 2
    assert accepted["checkpoint"]["appeal_id"] == submitted["appeal_id"]
    assert accepted["checkpoint"]["prior_decision_id"] == submitted["decision_id"]
    assert accepted["checkpoint"]["new_evidence_ids"] == submitted["evidence_ids"]
    assert accepted["checkpoint"]["dirty_claim_ids"] == ["claim_clear_mismatch_buyer"]


def test_seeded_appeal_creates_v2_and_preserves_v1_history(context) -> None:
    appeals = AppealService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
    )
    accepted = appeals.accept(
        "case_appeal_reversal",
        appeal_id="appeal_appeal_reversal_001",
        reviewer_id="user_reviewer_demo",
        reason="第三方原报告属于原决定未审查的关键新证据。",
    )
    assert accepted["state"] == "UNDER_INVESTIGATION"
    assert accepted["run_number"] == 1

    workflow = InvestigationWorkflow(
        context.sessions,
        orchestrator=context.orchestrator,
        runtime=AgentRuntime(context.sessions, tools=context.tools),
        tools=context.tools,
    )
    result = workflow.run_until_blocked("case_appeal_reversal")
    assert result["state"] == "HUMAN_REVIEW"

    with context.sessions() as session:
        decisions = list(
            session.scalars(
                select(Decision)
                .where(Decision.dispute_id == "case_appeal_reversal")
                .order_by(Decision.version)
            )
        )
        events = list(
            session.scalars(
                select(CaseEvent)
                .where(CaseEvent.dispute_id == "case_appeal_reversal")
                .order_by(CaseEvent.sequence)
            )
        )
        runs = list(
            session.scalars(
                select(CaseRun)
                .where(CaseRun.dispute_id == "case_appeal_reversal")
                .order_by(CaseRun.run_number)
            )
        )
        original_appeal = session.get(Appeal, "appeal_appeal_reversal_001")

    assert [item.version for item in decisions] == [1, 2]
    assert decisions[0].id == "decision_appeal_reversal_v1"
    assert decisions[0].status == "APPROVED"
    assert decisions[1].supersedes_decision_id == decisions[0].id
    assert [item.run_number for item in runs] == [1]
    assert original_appeal is not None and original_appeal.status == "ACCEPTED"
    assert [item.event_type for item in events[:7]] == [
        "BASELINE_CAPTURED",
        "INVESTIGATION_STARTED",
        "INVESTIGATION_COMPLETED",
        "REVIEW_PACKAGE_SUBMITTED",
        "DECISION_APPROVED",
        "NO_EXECUTION_REQUIRED",
        "VALID_APPEAL_RECEIVED",
    ]
    assert {item.event_type for item in events[7:]} >= {
        "APPEAL_ACCEPTED",
        "REINVESTIGATION_STARTED",
        "INVESTIGATION_COMPLETED",
        "REVIEW_PACKAGE_SUBMITTED",
    }


def test_non_material_appeal_cannot_reopen_but_can_be_denied_and_closed(context) -> None:
    resolve_clear_case(context)
    appeals = AppealService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
    )
    submitted = appeals.submit(
        "case_clear_mismatch",
        appellant_id="user_buyer_demo",
        appellant_role="BUYER",
        grounds="OTHER",
        statement="仅重复原陈述，不提供新的事实或错误线索。",
    )
    with pytest.raises(ConflictError, match="不能重开"):
        appeals.accept(
            "case_clear_mismatch",
            appeal_id=submitted["appeal_id"],
            reviewer_id="user_reviewer_demo",
            reason="尝试接受非实质申诉。",
        )
    denied = appeals.deny(
        "case_clear_mismatch",
        appeal_id=submitted["appeal_id"],
        reviewer_id="user_reviewer_demo",
        reason="没有新证据或可验证的重大错误。",
    )
    assert denied["state"] == "CLOSED"
    assert denied["status"] == "DENIED"


def test_expired_appeal_window_closes_resolved_case(context) -> None:
    resolve_clear_case(context)
    with context.sessions() as session:
        resolved_event = session.scalar(
            select(CaseEvent)
            .where(CaseEvent.dispute_id == "case_clear_mismatch", CaseEvent.to_state == "RESOLVED")
            .order_by(CaseEvent.sequence.desc())
            .limit(1)
        )
        assert resolved_event is not None
        close_at = resolved_event.occurred_at + timedelta(hours=121)

    appeals = AppealService(
        context.sessions,
        settings=context.settings,
        state_machine=context.state_machine,
    )
    closed = appeals.close_expired_window("case_clear_mismatch", now=close_at)
    assert closed["state"] == "CLOSED"
    with context.sessions() as session:
        assert session.scalar(
            select(func.count()).select_from(Appeal).where(Appeal.dispute_id == "case_clear_mismatch")
        ) == 0
