from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel

from dispute_agent.config import Settings, get_settings
from dispute_agent.errors import ModelBackendError, ValidationError
from dispute_agent.services.agent_store import estimate_usage


PayloadT = TypeVar("PayloadT", bound=BaseModel)


@dataclass(frozen=True)
class GenerationResult:
    payload: BaseModel
    model_name: str
    usage: dict[str, Any]


class StructuredGenerationBackend(Protocol):
    name: str

    def describe(self) -> dict[str, Any]: ...

    def health_check(self, *, probe: bool = True) -> dict[str, Any]: ...

    def generate(
        self,
        *,
        role: str,
        system_prompt: str,
        input_data: dict[str, Any],
        output_model: type[PayloadT],
        deterministic_builder: Callable[[dict[str, Any]], PayloadT],
    ) -> GenerationResult: ...


class RuleBasedGenerationBackend:
    """Offline, deterministic baseline used for repeatable demos and evaluation."""

    name = "rule-based-baseline-v1"

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "rule_based",
            "name": self.name,
            "model_name": self.name,
            "remote": False,
            "structured_output": True,
        }

    def health_check(self, *, probe: bool = True) -> dict[str, Any]:
        del probe
        return {**self.describe(), "status": "ok", "detail": "本地确定性规则后端可用"}

    def generate(
        self,
        *,
        role: str,
        system_prompt: str,
        input_data: dict[str, Any],
        output_model: type[PayloadT],
        deterministic_builder: Callable[[dict[str, Any]], PayloadT],
    ) -> GenerationResult:
        del role, system_prompt
        payload = output_model.model_validate(deterministic_builder(input_data))
        return GenerationResult(payload=payload, model_name=self.name, usage=estimate_usage(input_data, payload))


class CallableStructuredBackend:
    """Adapter point for an in-process provider returning schema-compatible JSON."""

    def __init__(
        self,
        name: str,
        generate_json: Callable[[str, str, dict[str, Any], dict[str, Any]], dict[str, Any]],
    ):
        self.name = name
        self.generate_json = generate_json

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "callable",
            "name": self.name,
            "model_name": self.name,
            "remote": False,
            "structured_output": True,
        }

    def health_check(self, *, probe: bool = True) -> dict[str, Any]:
        del probe
        return {**self.describe(), "status": "ok", "detail": "进程内结构化生成后端已加载"}

    def generate(
        self,
        *,
        role: str,
        system_prompt: str,
        input_data: dict[str, Any],
        output_model: type[PayloadT],
        deterministic_builder: Callable[[dict[str, Any]], PayloadT],
    ) -> GenerationResult:
        del deterministic_builder
        try:
            raw = self.generate_json(role, system_prompt, input_data, output_model.model_json_schema())
            payload = output_model.model_validate(raw)
        except Exception as exc:
            if isinstance(exc, ModelBackendError):
                raise
            raise ModelBackendError(f"结构化生成后端 {self.name} 调用失败: {exc}") from exc
        return GenerationResult(payload=payload, model_name=self.name, usage=estimate_usage(input_data, payload))


HttpPost = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]
HttpGet = Callable[[str, dict[str, str], float], dict[str, Any]]
Sleep = Callable[[float], None]


class _RetryableRequestError(Exception):
    pass


class OpenAICompatibleBackend:
    """Structured-output adapter for OpenAI-compatible chat-completions APIs."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        health_timeout_seconds: float = 5.0,
        temperature: float = 0.0,
        max_tokens: int = 8000,
        response_format: str = "auto",
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        schema_repair_attempts: int = 1,
        post_json: HttpPost | None = None,
        get_json: HttpGet | None = None,
        sleep: Sleep | None = None,
    ):
        if not base_url.strip():
            raise ValidationError("OpenAI-compatible 后端缺少 XIANYU_MODEL_BASE_URL")
        if not model.strip():
            raise ValidationError("OpenAI-compatible 后端缺少 XIANYU_MODEL_NAME")
        response_format = response_format.strip().lower()
        if response_format not in {"auto", "json_schema", "json_object", "none"}:
            raise ValidationError("model_response_format 必须是 auto、json_schema、json_object 或 none")
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.api_key = api_key or None
        self.timeout_seconds = timeout_seconds
        self.health_timeout_seconds = health_timeout_seconds
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.response_format = response_format
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.schema_repair_attempts = schema_repair_attempts
        self._post_json = post_json or self._urllib_post_json
        self._get_json = get_json or self._urllib_get_json
        self._sleep = sleep or time.sleep
        self._effective_response_format: str | None = None
        self.name = f"openai-compatible:{self.model}"

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "openai_compatible",
            "name": self.name,
            "model_name": self.model,
            "base_url": self.base_url,
            "chat_completions_url": self._endpoint(),
            "response_format": self.response_format,
            "effective_response_format": self._effective_response_format,
            "response_format_strategy": self._response_format_candidates(),
            "remote": True,
            "structured_output": True,
            "authentication_configured": bool(self.api_key),
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "schema_repair_attempts": self.schema_repair_attempts,
        }

    def health_check(self, *, probe: bool = True) -> dict[str, Any]:
        description = self.describe()
        if not probe:
            return {**description, "status": "not_probed", "detail": "模型已配置，未发起远程探测"}
        started = time.monotonic()
        try:
            response = self._get_json(self._models_endpoint(), self._headers(), self.health_timeout_seconds)
            available_models = self._model_ids(response)
            return {
                **description,
                "status": "ok",
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "configured_model_available": self.model in available_models if available_models else None,
                "available_model_count": len(available_models),
            }
        except Exception as exc:
            return {
                **description,
                "status": "unavailable",
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "detail": self._redact(str(exc)),
            }

    def generate(
        self,
        *,
        role: str,
        system_prompt: str,
        input_data: dict[str, Any],
        output_model: type[PayloadT],
        deterministic_builder: Callable[[dict[str, Any]], PayloadT],
    ) -> GenerationResult:
        # The deterministic builder is intentionally never used by this backend. A failed
        # model call must be visible to the harness and reviewer instead of silently changing
        # the decision source.
        del deterministic_builder
        schema = output_model.model_json_schema()
        last_format_error: ModelBackendError | None = None
        for response_format in self._response_format_candidates():
            try:
                result = self._generate_with_response_format(
                    role=role,
                    system_prompt=system_prompt,
                    input_data=input_data,
                    output_model=output_model,
                    schema=schema,
                    response_format=response_format,
                )
            except ModelBackendError as exc:
                if self.response_format == "auto" and self._response_format_unavailable(exc):
                    last_format_error = exc
                    continue
                raise
            self._effective_response_format = response_format
            return result
        raise ModelBackendError(
            "模型服务不支持 json_schema、json_object 或 prompt-only JSON 输出模式: "
            f"{self._redact(str(last_format_error))}"
        ) from last_format_error

    def _generate_with_response_format(
        self,
        *,
        role: str,
        system_prompt: str,
        input_data: dict[str, Any],
        output_model: type[PayloadT],
        schema: dict[str, Any],
        response_format: str,
    ) -> GenerationResult:
        repair_context: dict[str, str] | None = None
        last_error: Exception | None = None

        for schema_attempt in range(self.schema_repair_attempts + 1):
            request_payload = self._request_payload(
                role=role,
                system_prompt=system_prompt,
                input_data=input_data,
                schema=schema,
                output_model_name=output_model.__name__,
                repair_context=repair_context,
                response_format=response_format,
            )
            response = self._request_with_retries(request_payload)
            try:
                raw = self._extract_json(response)
                payload = output_model.model_validate(raw)
            except Exception as exc:
                last_error = exc
                if schema_attempt >= self.schema_repair_attempts:
                    break
                repair_context = {
                    "previous_response": self._response_excerpt(response),
                    "validation_error": self._redact(str(exc))[:4000],
                }
                continue
            return GenerationResult(
                payload=payload,
                model_name=self.name,
                usage=self._normalize_usage(response, input_data, payload),
            )

        raise ModelBackendError(
            f"模型输出在 {self.schema_repair_attempts + 1} 次结构化尝试后仍未通过 "
            f"{output_model.__name__} Schema 校验: {self._redact(str(last_error))}"
        ) from last_error

    def _request_payload(
        self,
        *,
        role: str,
        system_prompt: str,
        input_data: dict[str, Any],
        schema: dict[str, Any],
        output_model_name: str,
        repair_context: dict[str, str] | None,
        response_format: str,
    ) -> dict[str, Any]:
        user_payload: dict[str, Any] = {
            "task": "根据 input 生成严格符合 output_schema 的 JSON。只输出 JSON，不要 Markdown 或解释。",
            "agent_role": role,
            "input": input_data,
            "output_schema": schema,
        }
        if repair_context:
            user_payload.update(
                {
                    "task": "上一次输出无法解析或未通过 Schema。请根据错误重新生成完整 JSON，不要只返回补丁。",
                    "repair": repair_context,
                }
            )
        request_payload: dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"), default=str),
                },
            ],
        }
        schema_name = re.sub(r"[^a-zA-Z0-9_-]", "_", output_model_name)[:64]
        if response_format == "json_schema":
            request_payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
        elif response_format == "json_object":
            request_payload["response_format"] = {"type": "json_object"}
        return request_payload

    def _response_format_candidates(self) -> list[str]:
        if self.response_format != "auto":
            return [self.response_format]
        candidates = ["json_schema", "json_object", "none"]
        if self._effective_response_format in candidates:
            candidates.remove(self._effective_response_format)
            candidates.insert(0, self._effective_response_format)
        return candidates

    @staticmethod
    def _response_format_unavailable(exc: ModelBackendError) -> bool:
        text = str(exc).casefold()
        mentions_format = "response_format" in text or "json_schema" in text or "json_object" in text
        unavailable = any(
            phrase in text
            for phrase in (
                "unavailable",
                "unsupported",
                "not supported",
                "does not support",
                "invalid response format",
                "unknown response format",
            )
        )
        return mentions_format and unavailable

    def _request_with_retries(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._post_json(
                    self._endpoint(),
                    self._headers(),
                    deepcopy(request_payload),
                    self.timeout_seconds,
                )
                if not isinstance(response, dict):
                    raise ModelBackendError("模型接口响应根节点必须是对象")
                return response
            except ModelBackendError:
                raise
            except (_RetryableRequestError, URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                delay = self.retry_backoff_seconds * (2**attempt)
                if delay > 0:
                    self._sleep(delay)
            except Exception as exc:
                raise ModelBackendError(f"模型接口调用失败: {self._redact(str(exc))}") from exc
        raise ModelBackendError(
            f"模型接口传输失败，已尝试 {self.max_retries + 1} 次: {self._redact(str(last_error))}"
        ) from last_error

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _endpoint(self) -> str:
        return self.base_url if self.base_url.endswith("/chat/completions") else f"{self.base_url}/chat/completions"

    def _models_endpoint(self) -> str:
        suffix = "/chat/completions"
        root = self.base_url[: -len(suffix)] if self.base_url.endswith(suffix) else self.base_url
        return f"{root.rstrip('/')}/models"

    @staticmethod
    def _model_ids(response: dict[str, Any]) -> list[str]:
        data = response.get("data")
        if not isinstance(data, list):
            return []
        return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]

    @classmethod
    def _extract_json(cls, response: dict[str, Any]) -> dict[str, Any]:
        try:
            message = response["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelBackendError("模型响应缺少 choices[0].message") from exc
        parsed = message.get("parsed") if isinstance(message, dict) else None
        if isinstance(parsed, dict):
            return parsed
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(
                str(item.get("text") or item.get("content") or "")
                for item in content
                if isinstance(item, dict)
            )
        if isinstance(content, dict):
            return content
        if not isinstance(content, str) or not content.strip():
            raise ModelBackendError("模型响应没有可解析的 JSON content")

        text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1).strip()
        try:
            value = json.loads(text)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

        decoder = json.JSONDecoder()
        for match in re.finditer(r"\{", text):
            try:
                value, _ = decoder.raw_decode(text[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise ModelBackendError("模型 content 中没有可解析的 JSON 对象")

    @staticmethod
    def _response_excerpt(response: dict[str, Any]) -> str:
        try:
            content = response["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            content = response
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        return content[:8000]

    @staticmethod
    def _normalize_usage(
        response: dict[str, Any],
        input_data: dict[str, Any],
        payload: BaseModel,
    ) -> dict[str, Any]:
        estimates = estimate_usage(input_data, payload)
        provider = response.get("usage") if isinstance(response.get("usage"), dict) else {}

        def integer(*keys: str) -> int | None:
            for key in keys:
                value = provider.get(key)
                if isinstance(value, (int, float)) and value >= 0:
                    return int(value)
            return None

        prompt = integer("prompt_tokens", "input_tokens")
        completion = integer("completion_tokens", "output_tokens")
        total = integer("total_tokens")
        if total is None and prompt is not None and completion is not None:
            total = prompt + completion
        normalized: dict[str, Any] = dict(provider)
        if prompt is not None:
            normalized["prompt_tokens"] = prompt
            normalized["input_tokens_estimate"] = prompt
        else:
            normalized["input_tokens_estimate"] = estimates["input_tokens_estimate"]
        if completion is not None:
            normalized["completion_tokens"] = completion
            normalized["output_tokens_estimate"] = completion
        else:
            normalized["output_tokens_estimate"] = estimates["output_tokens_estimate"]
        if total is not None:
            normalized["total_tokens"] = total
            normalized["total_tokens_estimate"] = total
        else:
            normalized["total_tokens_estimate"] = estimates["total_tokens_estimate"]
        return normalized

    def _redact(self, value: str) -> str:
        if self.api_key:
            return value.replace(self.api_key, "***")
        return value

    @staticmethod
    def _urllib_post_json(
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return OpenAICompatibleBackend._execute_request(request, timeout_seconds)

    @staticmethod
    def _urllib_get_json(url: str, headers: dict[str, str], timeout_seconds: float) -> dict[str, Any]:
        request = Request(url, headers=headers, method="GET")
        return OpenAICompatibleBackend._execute_request(request, timeout_seconds)

    @staticmethod
    def _execute_request(request: Request, timeout_seconds: float) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            message = f"模型接口 HTTP {exc.code}: {detail}"
            if exc.code in {408, 409, 425, 429} or exc.code >= 500:
                raise _RetryableRequestError(message) from exc
            raise ModelBackendError(message) from exc
        except URLError as exc:
            raise _RetryableRequestError(f"无法连接模型接口: {exc.reason}") from exc
        except TimeoutError as exc:
            raise _RetryableRequestError("模型接口请求超时") from exc
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ModelBackendError(f"模型接口返回的不是 JSON: {exc}") from exc
        if not isinstance(result, dict):
            raise ModelBackendError("模型接口响应根节点必须是对象")
        return result


def create_generation_backend(settings: Settings | None = None) -> StructuredGenerationBackend:
    settings = settings or get_settings()
    backend = settings.model_backend.strip().lower().replace("-", "_")
    has_base_url = bool(settings.model_base_url and settings.model_base_url.strip())
    has_model_name = bool(settings.model_name and settings.model_name.strip())

    if backend == "auto":
        if has_base_url and has_model_name:
            backend = "openai_compatible"
        elif not has_base_url and not has_model_name:
            backend = "rule_based"
        else:
            missing = "XIANYU_MODEL_NAME" if has_base_url else "XIANYU_MODEL_BASE_URL"
            raise ValidationError(f"模型配置不完整：缺少 {missing}")

    if backend in {"rule_based", "rule", "baseline"}:
        return RuleBasedGenerationBackend()
    if backend in {"openai_compatible", "openai", "compatible"}:
        return OpenAICompatibleBackend(
            base_url=settings.model_base_url or "",
            model=settings.model_name or "",
            api_key=settings.model_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            health_timeout_seconds=settings.model_health_timeout_seconds,
            temperature=settings.model_temperature,
            max_tokens=settings.model_max_tokens,
            response_format=settings.model_response_format,
            max_retries=settings.model_max_retries,
            retry_backoff_seconds=settings.model_retry_backoff_seconds,
            schema_repair_attempts=settings.model_schema_repair_attempts,
        )
    raise ValidationError(f"未知 model_backend: {settings.model_backend}")
