from __future__ import annotations

import asyncio

import httpx
import pytest

from dispute_agent.agents.runtime import AgentRuntime
from dispute_agent.api import create_app
from dispute_agent.errors import NotFoundError, ValidationError
from dispute_agent.skills import SkillRegistry, get_skill_registry
from dispute_agent.skills.builtin import BUILTIN_SKILLS


def test_builtin_skill_registry_is_versioned_and_complete() -> None:
    registry = get_skill_registry()
    manifests = registry.manifests()
    assert {(item.name, item.version) for item in manifests} == {
        ("description-mismatch", "1.0.0"),
        ("missing-parts", "1.0.0"),
        ("empty-package", "1.0.0"),
        ("shipping-damage", "1.0.0"),
    }
    assert registry.get("description-mismatch").supports("DESCRIPTION_MISMATCH", "CONFIG_MISMATCH")
    assert not registry.get("description-mismatch").supports("EMPTY_PACKAGE", "PACKAGE_EMPTY")
    assert registry.for_dispute_type("MISSING_PARTS")[0].manifest.name == "missing-parts"

    for manifest in manifests:
        assert manifest.required_context
        assert manifest.allowed_tools
        assert manifest.evidence_requirements
        assert manifest.investigation_steps
        assert manifest.question_rules
        assert manifest.allowed_outcomes
        assert manifest.guard_profile.human_review_required is True
        assert manifest.guard_profile.automatic_final_decision_allowed is False


def test_skill_registry_rejects_duplicates_and_unknown_bindings() -> None:
    registry = SkillRegistry(BUILTIN_SKILLS)
    with pytest.raises(ValidationError, match="Skill 已注册"):
        registry.register(BUILTIN_SKILLS[0])
    with pytest.raises(NotFoundError, match="Skill 不存在"):
        registry.get("unknown-skill", "1.0.0")
    with pytest.raises(ValidationError, match="必须同时存在"):
        registry.resolve_bound_skill(name="description-mismatch", version=None)


def test_claim_routes_are_locked_into_case_run_and_agent_context(context) -> None:
    run = context.orchestrator.start("case_clear_mismatch")
    routes = run["checkpoint"]["phase_results"]["CLAIM_EXTRACTION"]["claim_routes"]
    assert routes
    assert all(item["issue_type"] == "DESCRIPTION_MISMATCH" for item in routes)
    assert all(item["routing_status"] == "ROUTED" for item in routes)
    assert all(item["skill_name"] == "description-mismatch" for item in routes)
    assert all(item["skill_version"] == "1.0.0" for item in routes)

    runtime = AgentRuntime(context.sessions, tools=context.tools)
    agent_context = runtime._build_investigation_context(  # noqa: SLF001 - verifies the persisted Agent contract
        "case_clear_mismatch",
        run["case_run_id"],
        role="BUYER_CASE_ANALYST",
        include_policy=False,
    )
    assert agent_context["claim_skill_bindings"]
    assert [item["name"] for item in agent_context["bound_skills"]] == ["description-mismatch"]
    assert agent_context["bound_skills"][0]["policy_scope"]["policy_ids"] == [
        "marketplace.description_mismatch"
    ]


def test_taxonomy_and_skill_catalog_are_exposed_by_api(context) -> None:
    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=create_app(context.sessions))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            taxonomy = (await client.get("/dispute-types")).json()
            assert "DESCRIPTION_MISMATCH" in taxonomy["dispute_types"]
            assert "SHIPPING_DAMAGE" in taxonomy["dispute_types"]
            assert "DETERMINISTIC_RULE" in taxonomy["routing_sources"]

            response = await client.get("/skills", params={"dispute_type": "EMPTY_PACKAGE"})
            assert response.status_code == 200
            assert [item["name"] for item in response.json()] == ["empty-package"]

            detail = await client.get("/skills/description-mismatch", params={"version": "1.0.0"})
            assert detail.status_code == 200
            assert detail.json()["guard_profile"]["automatic_final_decision_allowed"] is False

    asyncio.run(exercise_api())

