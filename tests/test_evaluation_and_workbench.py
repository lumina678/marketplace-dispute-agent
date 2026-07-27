from __future__ import annotations

import asyncio

import httpx

from dispute_agent.api import create_app
from dispute_agent.agents.runtime import AgentRuntime
from dispute_agent.agents.workflow import InvestigationWorkflow
from dispute_agent.services.evaluation import EvaluationService
from dispute_agent.services.workbench import WorkbenchService


def test_evaluation_harness_is_read_only_and_reports_expected_metrics(context) -> None:
    before = EvaluationService(context.sessions).evaluate()
    assert before["cases_total"] == 5
    assert set(before["metrics"]) >= {
        "outcome_accuracy",
        "policy_citation_accuracy",
        "key_evidence_recall",
        "unsupported_fact_ratio",
        "question_accuracy",
        "human_review_accuracy",
        "party_bias_rate",
        "duplicate_refund_count",
        "unauthorized_execution_rate",
        "appeal_correction_rate",
        "cost",
    }

    workflow = InvestigationWorkflow(
        context.sessions,
        orchestrator=context.orchestrator,
        runtime=AgentRuntime(context.sessions, tools=context.tools),
        tools=context.tools,
    )
    workflow.run_until_blocked("case_clear_mismatch")
    after = EvaluationService(context.sessions).evaluate(case_ids=["case_clear_mismatch"])
    assert after["cases_total"] == 1
    assert after["cases"][0]["actual_outcome"] == "RETURN_AND_FULL_REFUND"
    assert after["metrics"]["outcome_accuracy"]["value"] == 1.0


def test_workbench_projection_contains_columns_timeline_and_evidence_graph(context) -> None:
    workflow = InvestigationWorkflow(
        context.sessions,
        orchestrator=context.orchestrator,
        runtime=AgentRuntime(context.sessions, tools=context.tools),
        tools=context.tools,
    )
    workflow.run_until_blocked("case_clear_mismatch")
    workbench = WorkbenchService(context.sessions).get("case_clear_mismatch")
    assert workbench["case"]["state"] == "HUMAN_REVIEW"
    assert workbench["claims"]
    assert workbench["routing"]["ready_for_investigation"] is True
    assert workbench["routing"]["skill_bindings"] == ["description-mismatch@1.0.0"]
    assert workbench["evidence"]
    assert workbench["timeline"]
    assert workbench["recommendation"]["outcome"] == "RETURN_AND_FULL_REFUND"
    assert any(item["type"] == "claim" for item in workbench["evidence_graph"]["nodes"])
    assert any(item["relation"] == "RELATED_TO" for item in workbench["evidence_graph"]["edges"])


def test_workbench_and_evaluation_api_endpoints(context) -> None:
    async def exercise() -> None:
        transport = httpx.ASGITransport(app=create_app(context.sessions))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            evaluation = await client.get("/evaluation", params={"case_id": "case_clear_mismatch"})
            assert evaluation.status_code == 200
            assert evaluation.json()["cases_total"] == 1
            workbench = await client.get("/cases/case_clear_mismatch/workbench")
            assert workbench.status_code == 200
            assert workbench.json()["case"]["case_id"] == "case_clear_mismatch"
            page = await client.get("/workbench")
            assert page.status_code == 200
            assert "案件工作台" in page.text

    asyncio.run(exercise())
