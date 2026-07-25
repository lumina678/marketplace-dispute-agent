from __future__ import annotations

import asyncio
import json
from typing import Literal
from urllib.error import URLError

import httpx
import pytest
from pydantic import BaseModel

from dispute_agent.agents.backend import (
    CallableStructuredBackend,
    OpenAICompatibleBackend,
    RuleBasedGenerationBackend,
    create_generation_backend,
)
from dispute_agent.agents.heuristics import (
    build_decision_recommendation,
    build_evidence_policy_report,
    build_party_analysis,
)
from dispute_agent.api import create_app
from dispute_agent.config import Settings
from dispute_agent.errors import ModelBackendError, ValidationError


class TinyOutput(BaseModel):
    value: Literal["ok"]


def _settings(**overrides) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(_env_file=None, **overrides)


def _generate(backend: OpenAICompatibleBackend, builder=None):  # type: ignore[no-untyped-def]
    return backend.generate(
        role="TEST_AGENT",
        system_prompt="Return structured data.",
        input_data={"case_id": "case-test"},
        output_model=TinyOutput,
        deterministic_builder=builder or (lambda _data: TinyOutput(value="ok")),
    )


def test_auto_backend_selection_and_partial_configuration_rejection() -> None:
    assert isinstance(create_generation_backend(_settings(model_backend="auto")), RuleBasedGenerationBackend)

    configured = create_generation_backend(
        _settings(
            model_backend="auto",
            model_base_url="http://model.local/v1",
            model_api_key="secret",
            model_name="private-model",
        )
    )
    assert isinstance(configured, OpenAICompatibleBackend)
    assert configured.name == "openai-compatible:private-model"

    with pytest.raises(ValidationError, match="XIANYU_MODEL_NAME"):
        create_generation_backend(
            _settings(model_backend="auto", model_base_url="http://model.local/v1", model_name=None)
        )
    with pytest.raises(ValidationError, match="XIANYU_MODEL_BASE_URL"):
        create_generation_backend(_settings(model_backend="auto", model_base_url=None, model_name="private-model"))


def test_openai_compatible_request_url_headers_and_usage_normalization() -> None:
    calls: list[tuple[str, dict[str, str], dict, float]] = []

    def post_json(url, headers, payload, timeout):  # type: ignore[no-untyped-def]
        calls.append((url, headers, payload, timeout))
        return {
            "choices": [{"message": {"content": "```json\n{\"value\":\"ok\"}\n```"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        }

    backend = OpenAICompatibleBackend(
        base_url="http://model.local/v1",
        model="private-model",
        api_key="secret-key",
        timeout_seconds=9,
        post_json=post_json,
    )
    result = _generate(backend)

    assert calls[0][0] == "http://model.local/v1/chat/completions"
    assert calls[0][1]["Authorization"] == "Bearer secret-key"
    assert calls[0][2]["response_format"]["type"] == "json_schema"
    assert calls[0][3] == 9
    assert result.payload.value == "ok"
    assert result.usage["total_tokens"] == 18
    assert result.usage["total_tokens_estimate"] == 18
    assert result.usage["input_tokens_estimate"] == 11
    assert result.usage["output_tokens_estimate"] == 7


def test_transport_retries_then_succeeds_with_exponential_backoff() -> None:
    attempts = 0
    delays: list[float] = []

    def post_json(_url, _headers, _payload, _timeout):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise URLError("temporary failure")
        return {"choices": [{"message": {"content": '{"value":"ok"}'}}]}

    backend = OpenAICompatibleBackend(
        base_url="http://model.local/v1",
        model="private-model",
        max_retries=2,
        retry_backoff_seconds=0.25,
        post_json=post_json,
        sleep=delays.append,
    )
    assert _generate(backend).payload.value == "ok"
    assert attempts == 3
    assert delays == [0.25, 0.5]


def test_schema_failure_is_repaired_by_a_second_model_request() -> None:
    requests: list[dict] = []

    def post_json(_url, _headers, payload, _timeout):  # type: ignore[no-untyped-def]
        requests.append(payload)
        content = '{"value":"wrong"}' if len(requests) == 1 else '<think>fixed</think> 结果：{"value":"ok"}'
        return {"choices": [{"message": {"content": content}}]}

    backend = OpenAICompatibleBackend(
        base_url="http://model.local/v1",
        model="private-model",
        schema_repair_attempts=1,
        post_json=post_json,
    )
    assert _generate(backend).payload.value == "ok"
    assert len(requests) == 2
    repair_prompt = json.loads(requests[1]["messages"][1]["content"])
    assert "repair" in repair_prompt
    assert "validation_error" in repair_prompt["repair"]


def test_auto_response_format_falls_back_and_remembers_provider_capability() -> None:
    formats: list[str] = []

    def post_json(_url, _headers, payload, _timeout):  # type: ignore[no-untyped-def]
        response_format = payload.get("response_format", {}).get("type", "none")
        formats.append(response_format)
        if response_format == "json_schema":
            raise ModelBackendError(
                "模型接口 HTTP 400: This response_format type is unavailable now"
            )
        return {"choices": [{"message": {"content": '{"value":"ok"}'}}]}

    backend = OpenAICompatibleBackend(
        base_url="http://model.local/v1",
        model="private-model",
        response_format="auto",
        post_json=post_json,
    )
    assert _generate(backend).payload.value == "ok"
    assert formats == ["json_schema", "json_object"]
    assert backend.describe()["effective_response_format"] == "json_object"

    assert _generate(backend).payload.value == "ok"
    assert formats[-1] == "json_object"


def test_schema_repair_exhaustion_never_calls_deterministic_fallback() -> None:
    fallback_calls = 0

    def fallback(_data):  # type: ignore[no-untyped-def]
        nonlocal fallback_calls
        fallback_calls += 1
        return TinyOutput(value="ok")

    backend = OpenAICompatibleBackend(
        base_url="http://model.local/v1",
        model="private-model",
        schema_repair_attempts=1,
        post_json=lambda *_args: {"choices": [{"message": {"content": '{"value":"wrong"}'}}]},
    )
    with pytest.raises(ModelBackendError, match="2 次结构化尝试"):
        _generate(backend, builder=fallback)
    assert fallback_calls == 0


def test_health_check_uses_models_endpoint_and_never_leaks_api_key() -> None:
    calls: list[tuple[str, dict[str, str], float]] = []

    def healthy_get(url, headers, timeout):  # type: ignore[no-untyped-def]
        calls.append((url, headers, timeout))
        return {"data": [{"id": "private-model"}]}

    backend = OpenAICompatibleBackend(
        base_url="http://model.local/v1/chat/completions",
        model="private-model",
        api_key="top-secret",
        health_timeout_seconds=3,
        get_json=healthy_get,
    )
    result = backend.health_check()
    assert result["status"] == "ok"
    assert result["configured_model_available"] is True
    assert calls[0][0] == "http://model.local/v1/models"
    assert calls[0][1]["Authorization"] == "Bearer top-secret"
    assert calls[0][2] == 3
    assert "top-secret" not in json.dumps(result)

    backend = OpenAICompatibleBackend(
        base_url="http://model.local/v1",
        model="private-model",
        api_key="top-secret",
        get_json=lambda _url, headers, _timeout: (_ for _ in ()).throw(
            RuntimeError(f"provider rejected {headers['Authorization']}")
        ),
    )
    unhealthy = backend.health_check()
    assert unhealthy["status"] == "unavailable"
    assert "top-secret" not in json.dumps(unhealthy)


def test_fastapi_wires_custom_backend_and_exposes_model_and_full_payload(context) -> None:
    def generate_json(role, _system_prompt, input_data, _schema):  # type: ignore[no-untyped-def]
        if role in {"BUYER_CASE_ANALYST", "SELLER_CASE_ANALYST"}:
            payload = build_party_analysis(input_data)
        elif role == "EVIDENCE_POLICY_CLERK":
            payload = build_evidence_policy_report(input_data)
        else:
            payload = build_decision_recommendation(input_data)
        return payload.model_dump(mode="json")

    backend = CallableStructuredBackend("my-private-model", generate_json)

    async def exercise() -> None:
        app = create_app(context.sessions, settings=context.settings, generation_backend=backend)
        assert app.state.generation_backend is backend
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            model = await client.get("/model")
            assert model.status_code == 200
            assert model.json()["name"] == "my-private-model"
            health = await client.get("/model/health", params={"probe": "false"})
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

            workflow = await client.post("/cases/case_clear_mismatch/workflow")
            assert workflow.status_code == 200
            outputs = (await client.get("/cases/case_clear_mismatch/agent-outputs")).json()
            assert len(outputs) == 4
            assert {item["model_name"] for item in outputs} == {"my-private-model"}

            workbench = (await client.get("/cases/case_clear_mismatch/workbench")).json()
            assert all(item["payload"] for item in workbench["agent_trace"])
            assert all(item["usage"] for item in workbench["agent_trace"])

    asyncio.run(exercise())


def test_fake_openai_compatible_model_runs_the_complete_four_agent_workflow(context) -> None:
    roles: list[str] = []

    def post_json(_url, _headers, request_payload, _timeout):  # type: ignore[no-untyped-def]
        request = json.loads(request_payload["messages"][1]["content"])
        role = request["agent_role"]
        input_data = request["input"]
        roles.append(role)
        if role in {"BUYER_CASE_ANALYST", "SELLER_CASE_ANALYST"}:
            payload = build_party_analysis(input_data)
        elif role == "EVIDENCE_POLICY_CLERK":
            payload = build_evidence_policy_report(input_data)
        else:
            payload = build_decision_recommendation(input_data)
        return {
            "choices": [{"message": {"content": payload.model_dump_json()}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        }

    backend = OpenAICompatibleBackend(
        base_url="http://fake-model.local/v1",
        model="fake-private-model",
        api_key="test-only-key",
        post_json=post_json,
    )

    async def exercise() -> None:
        app = create_app(context.sessions, settings=context.settings, generation_backend=backend)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/cases/case_clear_mismatch/workflow")
            assert response.status_code == 200
            assert response.json()["workflow_boundary"] == "HUMAN_REVIEW_REQUIRED"
            outputs = (await client.get("/cases/case_clear_mismatch/agent-outputs")).json()
            assert len(outputs) == 4
            assert {item["model_name"] for item in outputs} == {
                "openai-compatible:fake-private-model"
            }
            assert all(item["usage"]["total_tokens_estimate"] == 150 for item in outputs)

    asyncio.run(exercise())
    assert set(roles) == {
        "BUYER_CASE_ANALYST",
        "SELLER_CASE_ANALYST",
        "EVIDENCE_POLICY_CLERK",
        "ADJUDICATION_AGENT",
    }


def test_fastapi_returns_502_and_keeps_case_paused_when_model_fails(context) -> None:
    def fail(*_args):  # type: ignore[no-untyped-def]
        raise RuntimeError("private provider unavailable")

    backend = CallableStructuredBackend("broken-private-model", fail)

    async def exercise() -> None:
        app = create_app(context.sessions, settings=context.settings, generation_backend=backend)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/cases/case_clear_mismatch/workflow")
            assert response.status_code == 502
            assert response.json()["error"]["code"] == "MODEL_BACKEND_ERROR"
            state = (await client.get("/cases/case_clear_mismatch")).json()
            assert state["state"] == "UNDER_INVESTIGATION"
            assert state["active_run"]["status"] == "PAUSED"
            assert state["active_run"]["phase"] == "PARTY_ANALYSIS"

    asyncio.run(exercise())
