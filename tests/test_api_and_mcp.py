from __future__ import annotations

import asyncio

import httpx

from dispute_agent.api import create_app
from dispute_agent.mcp_server import mcp


def test_fastapi_health_cases_and_start(context) -> None:
    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=create_app(context.sessions))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            cases = (await client.get("/cases")).json()
            assert len(cases) == 5
            response = await client.post("/cases/case_clear_mismatch/runs")
            assert response.status_code == 200
            assert response.json()["phase"] == "PARTY_ANALYSIS"
            state = (await client.get("/cases/case_clear_mismatch")).json()
            assert state["state"] == "UNDER_INVESTIGATION"

    asyncio.run(exercise_api())


def test_mcp_exposes_namespaced_tools() -> None:
    tool_names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {
        "transaction.get",
        "listing.get_snapshot",
        "conversation.search",
        "shipment.get_timeline",
        "evidence.list",
        "evidence.inspect",
        "policy.search",
        "policy.get_version",
        "case.get_state",
        "case.add_open_question",
        "resolution.create_draft",
        "resolution.execute_mock",
    } == tool_names


def test_fastapi_runs_steps_8_to_10_and_exposes_agent_outputs(context) -> None:
    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=create_app(context.sessions))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/cases/case_clear_mismatch/workflow")
            assert response.status_code == 202
            job = response.json()
            assert job["status"] == "COMPLETED", job["error"]
            result = job["result"]
            assert result["phase"] == "HUMAN_REVIEW"
            assert result["workflow_boundary"] == "HUMAN_REVIEW_REQUIRED"

            outputs = (
                await client.get(
                    "/cases/case_clear_mismatch/agent-outputs",
                    params={"case_run_id": result["case_run_id"]},
                )
            ).json()
            assert len(outputs) == 4
            adjudication = next(item for item in outputs if item["role"] == "ADJUDICATION_AGENT")
            detail = await client.get(
                f"/cases/case_clear_mismatch/agent-outputs/{adjudication['output_id']}"
            )
            assert detail.status_code == 200
            assert detail.json()["payload"]["outcome"] == "RETURN_AND_FULL_REFUND"

            review_package = await client.get("/cases/case_clear_mismatch/review-package")
            assert review_package.status_code == 200
            package = review_package.json()
            assert package["package_integrity_valid"] is True
            approval = await client.post(
                "/cases/case_clear_mismatch/reviews/approve",
                json={
                    "decision_id": package["decision"]["decision_id"],
                    "reviewer_id": "user_reviewer_demo",
                    "reason": "API 测试审核通过。",
                },
            )
            assert approval.status_code == 200
            assert approval.json()["state"] == "APPROVED"

    asyncio.run(exercise_api())
