from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import func, select

from dispute_agent.api import create_app
from dispute_agent.dispute_types import DisputeType
from dispute_agent.errors import AuthorizationError, ConflictError
from dispute_agent.models import Claim, ClaimRoutingDecision, Dispute
from dispute_agent.routing_schemas import ClaimRoutingHint, ModelRoutingCandidate
from dispute_agent.services.claim_router import ClaimRoutingService, DeterministicClaimRouter


def first_claim(context, case_id: str) -> Claim:  # type: ignore[no-untyped-def]
    with context.sessions() as session:
        claim = session.scalar(select(Claim).where(Claim.dispute_id == case_id).order_by(Claim.asserted_at))
        assert claim is not None
        session.expunge(claim)
        return claim


def mark_unrouted(context, case_id: str, *, statement: str, claim_type: str = "OTHER") -> str:  # type: ignore[no-untyped-def]
    with context.sessions() as session:
        claim = session.scalar(select(Claim).where(Claim.dispute_id == case_id).order_by(Claim.asserted_at))
        assert claim is not None
        claim.statement = statement
        claim.claim_type = claim_type
        claim.issue_type = "OTHER"
        claim.issue_subtype = None
        claim.routing_source = "USER_DECLARED"
        claim.routing_reason = None
        claim.routing_confidence = None
        claim.skill_name = None
        claim.skill_version = None
        claim.routing_status = "UNROUTED"
        session.commit()
        return claim.id


def test_router_precedence_is_deterministic(context) -> None:
    router = DeterministicClaimRouter(context.settings.router_config_path)
    declared = router.decide(
        statement="收到空包，而且怀疑是假货",
        current_claim_type="OTHER",
        hint=ClaimRoutingHint(
            declared_issue_type=DisputeType.SHIPPING_DAMAGE,
            platform_reason_code="EMPTY_PACKAGE",
            model_candidate=ModelRoutingCandidate(issue_type=DisputeType.COUNTERFEIT, confidence=0.99),
        ),
    )
    assert declared.issue_type == DisputeType.SHIPPING_DAMAGE
    assert declared.routing_source.value == "USER_DECLARED"
    assert declared.routing_status.value == "ROUTED"

    platform = router.decide(
        statement="收到空包",
        current_claim_type="OTHER",
        hint=ClaimRoutingHint(platform_reason_code="MISSING_ACCESSORY"),
    )
    assert platform.issue_type == DisputeType.MISSING_PARTS
    assert platform.routing_source.value == "PLATFORM_REASON_CODE"


def test_router_detects_ambiguity_and_never_auto_accepts_model_candidate(context) -> None:
    router = DeterministicClaimRouter(context.settings.router_config_path)
    ambiguous = router.decide(statement="收到的是空包，外包装破损", current_claim_type="OTHER")
    assert ambiguous.issue_type == DisputeType.EMPTY_PACKAGE
    assert ambiguous.routing_status.value == "NEEDS_HUMAN"
    assert ambiguous.requires_human_confirmation is True
    assert any(item.startswith("competing_issue_type:SHIPPING_DAMAGE") for item in ambiguous.matched_signals)

    model_only = router.decide(
        statement="情况比较复杂，无法从文字直接分类",
        current_claim_type="OTHER",
        hint=ClaimRoutingHint(
            model_candidate=ModelRoutingCandidate(
                issue_type=DisputeType.MISSING_PARTS,
                confidence=0.99,
                rationale="可能缺少配件",
            )
        ),
    )
    assert model_only.skill_name == "missing-parts"
    assert model_only.routing_source.value == "MODEL_SUGGESTION"
    assert model_only.routing_status.value == "NEEDS_HUMAN"


def test_counterfeit_and_unknown_routes_require_human(context) -> None:
    router = DeterministicClaimRouter(context.settings.router_config_path)
    counterfeit = router.decide(statement="买家怀疑收到的是高仿假货", current_claim_type="OTHER")
    assert counterfeit.issue_type == DisputeType.COUNTERFEIT
    assert counterfeit.skill_name is None
    assert counterfeit.routing_status.value == "NEEDS_HUMAN"

    unknown = router.decide(statement="我对这笔交易不满意", current_claim_type="OTHER")
    assert unknown.issue_type == DisputeType.OTHER
    assert unknown.confidence == 0
    assert unknown.routing_status.value == "NEEDS_HUMAN"


def test_routing_persistence_is_idempotent_and_reviewer_override_is_versioned(context) -> None:
    claim_id = mark_unrouted(context, "case_clear_mismatch", statement="卖家少发了充电器")
    service = ClaimRoutingService(context.sessions, settings=context.settings)

    first = service.route_claim("case_clear_mismatch", claim_id)
    assert first["route"]["issue_type"] == "MISSING_PARTS"
    assert first["route"]["skill_name"] == "missing-parts"
    assert first["route"]["decision_version"] == 1
    assert first["case_routing"]["ready_for_investigation"] is True

    repeated = service.route_claim("case_clear_mismatch", claim_id, force_recompute=True)
    assert repeated["route"]["decision_id"] == first["route"]["decision_id"]
    assert repeated["route"]["reused_current_binding"] is True

    overridden = service.override_claim(
        "case_clear_mismatch",
        claim_id,
        reviewer_id="user_reviewer_demo",
        issue_type=DisputeType.SHIPPING_DAMAGE,
        claim_type="ITEM_DAMAGED_IN_TRANSIT",
        reason="平台人工复核确认核心争议是运输造成的损坏。",
    )
    assert overridden["route"]["decision_version"] == 2
    assert overridden["route"]["routing_status"] == "OVERRIDDEN"
    assert overridden["route"]["skill_name"] == "shipping-damage"

    with context.sessions() as session:
        assert session.scalar(
            select(func.count()).select_from(ClaimRoutingDecision).where(ClaimRoutingDecision.claim_id == claim_id)
        ) == 2
        claim = session.get(Claim, claim_id)
        assert claim is not None
        assert claim.routing_source == "REVIEWER_OVERRIDE"
        assert claim.skill_name == "shipping-damage"


def test_routing_override_requires_registered_reviewer(context) -> None:
    claim = first_claim(context, "case_clear_mismatch")
    service = ClaimRoutingService(context.sessions, settings=context.settings)
    with pytest.raises(AuthorizationError, match="REVIEWER 或 ADMIN"):
        service.override_claim(
            "case_clear_mismatch",
            claim.id,
            reviewer_id="user_buyer_demo",
            issue_type=DisputeType.MISSING_PARTS,
            claim_type="MISSING_ACCESSORY",
            reason="买方不能覆盖路由。",
        )


def test_orchestrator_auto_routes_unrouted_claim_and_locks_result(context) -> None:
    claim_id = mark_unrouted(context, "case_clear_mismatch", statement="实际内存和商品描述不符")
    run = context.orchestrator.start("case_clear_mismatch")
    route = run["checkpoint"]["phase_results"]["CLAIM_EXTRACTION"]["claim_routes"][0]
    assert route["claim_id"] == claim_id
    assert route["issue_type"] == "DESCRIPTION_MISMATCH"
    assert route["skill_name"] == "description-mismatch"
    assert route["routing_status"] == "ROUTED"

    with context.sessions() as session:
        record = session.scalar(select(ClaimRoutingDecision).where(ClaimRoutingDecision.claim_id == claim_id))
        assert record is not None
        assert record.case_run_id is None
        assert len(record.input_fingerprint) == 64
        assert len(record.content_sha256) == 64


def test_unresolved_route_is_persisted_and_blocks_agent_start(context) -> None:
    claim_id = mark_unrouted(context, "case_clear_mismatch", statement="我对这笔交易不满意")
    with pytest.raises(ConflictError, match="必须先完成人工分类"):
        context.orchestrator.start("case_clear_mismatch")

    with context.sessions() as session:
        dispute = session.get(Dispute, "case_clear_mismatch")
        claim = session.get(Claim, claim_id)
        record = session.scalar(select(ClaimRoutingDecision).where(ClaimRoutingDecision.claim_id == claim_id))
        assert dispute is not None and dispute.state == "SUBMITTED"
        assert claim is not None and claim.routing_status == "NEEDS_HUMAN"
        assert record is not None and record.requires_human_confirmation is True
        assert "ROUTING_REQUIRES_HUMAN" in dispute.human_review_reasons_json


def test_active_case_run_prevents_in_place_routing_change(context) -> None:
    run = context.orchestrator.start("case_clear_mismatch")
    claim = first_claim(context, "case_clear_mismatch")
    service = ClaimRoutingService(context.sessions, settings=context.settings)
    assert run["case_run_id"]
    with pytest.raises(ConflictError, match="活动调查期间"):
        service.route_claim(
            "case_clear_mismatch",
            claim.id,
            hint=ClaimRoutingHint(declared_issue_type=DisputeType.MISSING_PARTS),
            force_recompute=True,
        )


def test_routing_api_exposes_decisions_and_override(context) -> None:
    claim_id = mark_unrouted(context, "case_clear_mismatch", statement="少了配件和充电器")

    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=create_app(context.sessions, settings=context.settings))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            routed = await client.post(f"/cases/case_clear_mismatch/claims/{claim_id}/routing", json={})
            assert routed.status_code == 200
            assert routed.json()["route"]["issue_type"] == "MISSING_PARTS"

            view = await client.get("/cases/case_clear_mismatch/routing")
            assert view.status_code == 200
            assert view.json()["history"][0]["router_version"] == "1.0.0"

            override = await client.post(
                f"/cases/case_clear_mismatch/claims/{claim_id}/routing-override",
                json={
                    "reviewer_id": "user_reviewer_demo",
                    "issue_type": "DESCRIPTION_MISMATCH",
                    "claim_type": "OTHER_DESCRIPTION_MISMATCH",
                    "reason": "审核聊天后确认买方实际主张是商品描述不符。",
                },
            )
            assert override.status_code == 200
            assert override.json()["route"]["routing_status"] == "OVERRIDDEN"

    asyncio.run(exercise_api())
