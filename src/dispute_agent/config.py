from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XIANYU_", env_file=".env", extra="ignore")

    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'dispute_agent.db'}"
    policy_directory: Path = PROJECT_ROOT / "policies"
    state_machine_path: Path = PROJECT_ROOT / "config" / "case_state_machine.json"
    router_config_path: Path = PROJECT_ROOT / "config" / "dispute_router.json"
    max_question_rounds: int = 3
    default_tool_call_budget: int = 60
    default_token_budget: int = 40_000
    max_phase_failures: int = 2
    # "auto" keeps the deterministic baseline when no model is configured,
    # and switches to the OpenAI-compatible adapter when BASE_URL + NAME exist.
    model_backend: str = "auto"
    model_base_url: str | None = None
    model_api_key: str | None = None
    model_name: str | None = None
    model_timeout_seconds: float = Field(default=120.0, gt=0)
    model_health_timeout_seconds: float = Field(default=5.0, gt=0)
    model_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    model_max_tokens: int = Field(default=8000, gt=0)
    # "auto" prefers provider-side JSON Schema and automatically falls back to
    # json_object / prompt-only JSON when a compatible provider lacks that mode.
    model_response_format: str = "auto"
    model_max_retries: int = Field(default=2, ge=0, le=10)
    model_retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=60.0)
    model_schema_repair_attempts: int = Field(default=1, ge=0, le=5)
    # The HTTP process only enqueues workflow jobs. RQ workers execute model calls.
    # Tests may opt into the explicit inline adapter; production must use RQ.
    workflow_queue_backend: str = "rq"
    redis_url: str = "redis://127.0.0.1:6379/0"
    workflow_queue_name: str = "xianyu-workflow"
    workflow_job_timeout_seconds: int = Field(default=600, ge=30, le=7200)
    workflow_max_attempts: int = Field(default=3, ge=1, le=10)
    workflow_retry_delays_seconds: str = "5,30"
    workflow_heartbeat_interval_seconds: float = Field(default=5.0, ge=1.0, le=60.0)
    workflow_stale_after_seconds: int = Field(default=30, ge=5, le=600)
    workflow_sse_poll_interval_seconds: float = Field(default=0.5, ge=0.1, le=10.0)

    def workflow_retry_delays(self) -> list[int]:
        values = [item.strip() for item in self.workflow_retry_delays_seconds.split(",") if item.strip()]
        delays = [int(item) for item in values]
        if any(item < 0 or item > 3600 for item in delays):
            raise ValueError("workflow retry delays must be between 0 and 3600 seconds")
        return delays


@lru_cache
def get_settings() -> Settings:
    return Settings()
