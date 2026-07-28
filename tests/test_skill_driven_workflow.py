from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

import pytest
from sqlalchemy import select

from dispute_agent.agents.runtime import AgentRuntime
from dispute_agent.agents.workflow import InvestigationWorkflow
from dispute_agent.errors import ConflictError
from dispute_agent.models import Claim, DecisionGuardReport, Dispute, Evidence, OpenQuestion
from dispute_agent.services.tools import ToolService
from dispute_agent.skills import get_skill_registry


CASE_ID = "case_clear_mismatch"


class RecordingToolService(ToolService):
    def __init__(self, session_factory):  # type: ignore[no-untyped-def]
        super().__init__(session_factory)
        self.calls: list[str] = []

    def call(self, tool_name, parameters, *, actor, case_run_id=None):  # type: ignore[no-untyped-def]
        self.calls.append(tool_name)
        return super().call(
            tool_name,
            parameters,
            actor=actor,
            case_run_id=case_run_id,
        )


def configure_case(context, *, issue_type: str, claim_type: str, skill_name: str, statement: str) -> str:  # type: ignore[no-untyped-def]
    with context.sessions() as session:
        dispute = session.get(Dispute, CASE_ID)
        claims = list(session.scalars(select(Claim).where(Claim.dispute_id == CASE_ID)))
        assert dispute is not None and claims
        primary = claims[0]
        for extra in claims[1:]:
            session.delete(extra)
        session.query(Evidence).filter(Evidence.dispute_id == CASE_ID).delete(synchronize_session=False)
        dispute.dispute_type = issue_type
        dispute.policy_id = None
        dispute.policy_version = None
        dispute.policy_basis_time = None
        primary.party = "BUYER"
        primary.claim_type = claim_type
        primary.issue_type = issue_type
        primary.issue_subtype = claim_type
        primary.routing_source = "USER_DECLARED"
        primary.routing_reason = "第二十步 Skill 驱动工作流测试。"
        primary.routing_confidence = 1.0
        primary.routing_status = "ROUTED"
        primary.skill_name = skill_name
        primary.skill_version = "1.0.0"
        primary.statement = statement
        primary.material = True
        session.commit()
        return primary.id


def add_evidence(
    context,
    *,
    evidence_id: str,
    claim_id: str,
    submitted_by: str,
    evidence_type: str,
    facts: list[dict],
) -> None:  # type: ignore[no-untyped-def]
    now = datetime.now(timezone.utc)
    with context.sessions() as session:
        session.add(
            Evidence(
                id=evidence_id,
                dispute_id=CASE_ID,
                submitted_by=submitted_by,
                evidence_type=evidence_type,
                description=f"第二十步文本证据：{evidence_id}",
                source_system="TEST_FIXTURE",
                source_record_id=evidence_id,
                captured_at=now,
                submitted_at=now,
                content_sha256=sha256(evidence_id.encode()).hexdigest(),
                immutable_uri=f"fixture://{evidence_id}",
                integrity_status="SOURCE_VERIFIED",
                related_claim_ids_json=[claim_id],
                extracted_facts_json=facts,
                handling_flags_json=[],
            )
        )
        session.commit()


@pytest.mark.parametrize(
    ("issue_type", "claim_type", "skill_name", "policy_id"),
    [
        ("MISSING_PARTS", "MISSING_ACCESSORY", "missing-parts", "marketplace.missing_parts"),
        ("EMPTY_PACKAGE", "PACKAGE_EMPTY", "empty-package", "marketplace.empty_package"),
        ("SHIPPING_DAMAGE", "ITEM_DAMAGED_IN_TRANSIT", "shipping-damage", "marketplace.shipping_damage"),
    ],
)
def test_each_skill_selects_its_own_policy(
    context,
    issue_type: str,
    claim_type: str,
    skill_name: str,
    policy_id: str,
) -> None:
    configure_case(
        context,
        issue_type=issue_type,
        claim_type=claim_type,
        skill_name=skill_name,
        statement="测试对应争议类型。",
    )
    context.orchestrator.start(CASE_ID)
    with context.sessions() as session:
        dispute = session.get(Dispute, CASE_ID)
        assert dispute is not None
        assert (dispute.policy_id, dispute.policy_version) == (policy_id, "1.0.0")


def test_skill_context_controls_real_tool_calls(context) -> None:  # type: ignore[no-untyped-def]
    configure_case(
        context,
        issue_type="EMPTY_PACKAGE",
        claim_type="PACKAGE_EMPTY",
        skill_name="empty-package",
        statement="买方称签收后包裹为空。",
    )
    run = context.orchestrator.start(CASE_ID)
    tools = RecordingToolService(context.sessions)
    runtime = AgentRuntime(context.sessions, tools=tools)
    agent_context = runtime._build_investigation_context(  # noqa: SLF001
        CASE_ID,
        run["case_run_id"],
        role="BUYER_CASE_ANALYST",
        include_policy=False,
    )
    assert "listing" not in agent_context
    assert "listing_snapshot" not in agent_context["skill_execution"]["actual_context_resources"]
    assert "listing.get_snapshot" not in tools.calls
    assert agent_context["skill_execution"]["skill_name"] == "empty-package"

    skill = get_skill_registry().get("empty-package", "1.0.0")
    with pytest.raises(ConflictError, match="不允许调用工具"):
        runtime._require_tool_allowed(skill, "listing.get_snapshot")  # noqa: SLF001


def test_missing_parts_context_reads_listing_snapshot(context) -> None:  # type: ignore[no-untyped-def]
    configure_case(
        context,
        issue_type="MISSING_PARTS",
        claim_type="MISSING_ACCESSORY",
        skill_name="missing-parts",
        statement="买方称商品页约定的充电器未随货交付。",
    )
    run = context.orchestrator.start(CASE_ID)
    tools = RecordingToolService(context.sessions)
    agent_context = AgentRuntime(context.sessions, tools=tools)._build_investigation_context(  # noqa: SLF001
        CASE_ID,
        run["case_run_id"],
        role="BUYER_CASE_ANALYST",
        include_policy=False,
    )
    assert "listing" in agent_context
    assert "listing_snapshot" in agent_context["skill_execution"]["actual_context_resources"]
    assert "listing.get_snapshot" in tools.calls


@pytest.mark.parametrize(
    ("issue_type", "claim_type", "skill_name", "expected_text"),
    [
        ("MISSING_PARTS", "MISSING_ACCESSORY", "missing-parts", "签收时包裹内实际包含哪些物品"),
        ("EMPTY_PACKAGE", "PACKAGE_EMPTY", "empty-package", "包裹重量"),
        ("SHIPPING_DAMAGE", "ITEM_DAMAGED_IN_TRANSIT", "shipping-damage", "签收时外包装和商品状态"),
    ],
)
def test_each_skill_generates_distinct_targeted_questions(
    context,
    issue_type: str,
    claim_type: str,
    skill_name: str,
    expected_text: str,
) -> None:
    configure_case(
        context,
        issue_type=issue_type,
        claim_type=claim_type,
        skill_name=skill_name,
        statement="当前只有双方文本主张，关键事实尚缺。",
    )
    runtime = AgentRuntime(context.sessions, tools=context.tools)
    workflow = InvestigationWorkflow(
        context.sessions,
        orchestrator=context.orchestrator,
        runtime=runtime,
        tools=context.tools,
    )
    result = workflow.run_until_blocked(CASE_ID)
    assert result["workflow_boundary"] == "EVIDENCE_RESPONSE_REQUIRED"
    with context.sessions() as session:
        questions = list(
            session.scalars(
                select(OpenQuestion).where(OpenQuestion.dispute_id == CASE_ID, OpenQuestion.status == "OPEN")
            )
        )
    assert any(expected_text in item.question for item in questions)
    assert all(item.generation_reason.startswith("SKILL_RULE:") for item in questions)


def _prepare_complete_case(context, skill_name: str) -> None:  # type: ignore[no-untyped-def]
    if skill_name == "missing-parts":
        claim_id = configure_case(
            context,
            issue_type="MISSING_PARTS",
            claim_type="MISSING_ACCESSORY",
            skill_name=skill_name,
            statement="约定包含充电器，签收时缺少充电器。",
        )
        add_evidence(
            context,
            evidence_id="ev_step20_mp_system",
            claim_id=claim_id,
            submitted_by="SYSTEM",
            evidence_type="LISTING_SNAPSHOT",
            facts=[
                {"field": "promised_items", "value": ["笔记本", "充电器"]},
                {"field": "preshipment_items", "value": ["笔记本", "充电器"]},
                {"field": "suggested_refund_amount_minor", "value": 12000},
            ],
        )
        add_evidence(
            context,
            evidence_id="ev_step20_mp_buyer",
            claim_id=claim_id,
            submitted_by="BUYER",
            evidence_type="DOCUMENT",
            facts=[
                {"field": "received_items", "value": ["笔记本"]},
                {"field": "missing_items", "value": ["充电器"]},
            ],
        )
    elif skill_name == "empty-package":
        claim_id = configure_case(
            context,
            issue_type="EMPTY_PACKAGE",
            claim_type="PACKAGE_EMPTY",
            skill_name=skill_name,
            statement="买方称首次开包时没有交易商品。",
        )
        add_evidence(
            context,
            evidence_id="ev_step20_ep_system",
            claim_id=claim_id,
            submitted_by="SYSTEM",
            evidence_type="DOCUMENT",
            facts=[
                {"field": "pickup_weight_grams", "value": 2350},
                {"field": "waybill_id", "value": "WB-STEP20-EP"},
                {"field": "packing_contents", "value": ["笔记本"]},
            ],
        )
        add_evidence(
            context,
            evidence_id="ev_step20_ep_buyer",
            claim_id=claim_id,
            submitted_by="BUYER",
            evidence_type="DOCUMENT",
            facts=[{"field": "opening_contents", "value": []}],
        )
    else:
        claim_id = configure_case(
            context,
            issue_type="SHIPPING_DAMAGE",
            claim_type="ITEM_DAMAGED_IN_TRANSIT",
            skill_name=skill_name,
            statement="签收时外包装破损，商品屏幕碎裂。",
        )
        add_evidence(
            context,
            evidence_id="ev_step20_sd_seller",
            claim_id=claim_id,
            submitted_by="SELLER",
            evidence_type="DOCUMENT",
            facts=[
                {"field": "preshipment_condition", "value": "功能和外观完好"},
                {"field": "packaging_condition", "value": "仅使用薄纸箱"},
                {"field": "seller_packaging_inadequate", "value": True},
            ],
        )
        add_evidence(
            context,
            evidence_id="ev_step20_sd_buyer",
            claim_id=claim_id,
            submitted_by="BUYER",
            evidence_type="DOCUMENT",
            facts=[
                {"field": "delivery_condition", "value": "外包装破损"},
                {"field": "damage_condition", "value": "屏幕碎裂"},
                {"field": "damage_reported_at", "value": "签收后 30 分钟"},
            ],
        )


@pytest.mark.parametrize(
    ("skill_name", "expected_outcome"),
    [
        ("missing-parts", "PARTIAL_REFUND"),
        ("empty-package", "RETURN_AND_FULL_REFUND"),
        ("shipping-damage", "RETURN_AND_FULL_REFUND"),
    ],
)
def test_complete_text_case_reaches_skill_specific_guard_profile(
    context,
    skill_name: str,
    expected_outcome: str,
) -> None:
    _prepare_complete_case(context, skill_name)
    runtime = AgentRuntime(context.sessions, tools=context.tools)
    result = InvestigationWorkflow(
        context.sessions,
        orchestrator=context.orchestrator,
        runtime=runtime,
        tools=context.tools,
    ).run_until_blocked(CASE_ID)
    assert result["state"] == "HUMAN_REVIEW"
    with context.sessions() as session:
        guard = session.scalar(
            select(DecisionGuardReport).where(DecisionGuardReport.dispute_id == CASE_ID)
        )
        assert guard is not None
        assert guard.passed is True
        assert guard.checks_json["skill_name"] == skill_name
        assert guard.checks_json["skill_guard_profile"]["human_review_required"] is True
        assert result["checkpoint"]["phase_results"]["ADJUDICATION"]["decision_id"] == guard.decision_id
        decision_outcome = session.execute(
            select(Dispute.state).where(Dispute.id == CASE_ID)
        ).scalar_one()
        assert decision_outcome == "HUMAN_REVIEW"
    adjudication = next(
        item for item in runtime.list_outputs(CASE_ID, result["case_run_id"])
        if item.role == "ADJUDICATION_AGENT"
    )
    assert adjudication.payload["outcome"] == expected_outcome


@pytest.mark.parametrize(
    ("skill_name", "condition", "fact_field"),
    [
        ("missing-parts", "HIGH_VALUE_COMPONENT_MISSING", "high_value_component_missing"),
        ("empty-package", "WEIGHT_CHAIN_CONFLICT", "weight_chain_conflict"),
        ("shipping-damage", "LATE_DAMAGE_REPORT", "late_damage_report"),
    ],
)
def test_each_guard_profile_enforces_its_own_escalation_condition(
    context,
    skill_name: str,
    condition: str,
    fact_field: str,
) -> None:
    _prepare_complete_case(context, skill_name)
    with context.sessions() as session:
        claim = session.scalar(select(Claim).where(Claim.dispute_id == CASE_ID))
        evidence = session.scalar(select(Evidence).where(Evidence.dispute_id == CASE_ID))
        assert claim is not None and evidence is not None
        if skill_name == "missing-parts":
            claim.claim_type = "MISSING_COMPONENT"
            claim.issue_subtype = "MISSING_COMPONENT"
        evidence.extracted_facts_json = [
            *evidence.extracted_facts_json,
            {"field": fact_field, "value": True},
        ]
        session.commit()

    runtime = AgentRuntime(context.sessions, tools=context.tools)
    result = InvestigationWorkflow(
        context.sessions,
        orchestrator=context.orchestrator,
        runtime=runtime,
        tools=context.tools,
    ).run_until_blocked(CASE_ID)
    assert result["state"] == "HUMAN_REVIEW"
    adjudication = next(
        item for item in runtime.list_outputs(CASE_ID, result["case_run_id"])
        if item.role == "ADJUDICATION_AGENT"
    )
    assert adjudication.payload["outcome"] == "ESCALATE_TO_HUMAN"
    with context.sessions() as session:
        guard = session.scalar(select(DecisionGuardReport).where(DecisionGuardReport.dispute_id == CASE_ID))
        assert guard is not None and guard.passed is True
        assert guard.checks_json["matched_skill_escalation_conditions"] == [condition]
