from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(os.getenv("XIANYU_PROJECT_ROOT", Path(__file__).resolve().parents[2])).resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XIANYU_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "staging", "production"] = "development"
    release: str = "dev"
    public_base_url: str | None = None
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'dispute_agent.db'}"
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_recycle_seconds: int = Field(default=1800, ge=30, le=86400)
    policy_directory: Path = PROJECT_ROOT / "policies"
    state_machine_path: Path = PROJECT_ROOT / "config" / "case_state_machine.json"
    router_config_path: Path = PROJECT_ROOT / "config" / "dispute_router.json"
    web_directory: Path = PROJECT_ROOT / "web"
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
    readiness_require_worker: bool | None = None
    worker_ttl_seconds: int = Field(default=420, ge=60, le=3600)
    # Reviewer workbench authentication. The browser receives only an opaque
    # session id; reviewer identity and CSRF state live in Redis.
    auth_enabled: bool = True
    auth_session_cookie_name: str = "xianyu_reviewer_session"
    auth_session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)
    auth_session_redis_prefix: str = "xianyu:auth:session:"
    auth_cookie_secure: bool | None = None

    def workflow_retry_delays(self) -> list[int]:
        values = [item.strip() for item in self.workflow_retry_delays_seconds.split(",") if item.strip()]
        delays = [int(item) for item in values]
        if any(item < 0 or item > 3600 for item in delays):
            raise ValueError("workflow retry delays must be between 0 and 3600 seconds")
        return delays

    def should_require_worker_for_readiness(self) -> bool:
        if self.readiness_require_worker is not None:
            return self.readiness_require_worker
        return self.environment in {"staging", "production"}

    def should_use_secure_auth_cookie(self) -> bool:
        if self.auth_cookie_secure is not None:
            return self.auth_cookie_secure
        return self.environment in {"staging", "production"}

    @model_validator(mode="after")
    def validate_deployment_profile(self) -> "Settings":
        if self.environment not in {"staging", "production"}:
            return self
        if not self.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError(f"{self.environment} must use PostgreSQL")
        if self.workflow_queue_backend.strip().lower() != "rq":
            raise ValueError(f"{self.environment} must use the RQ workflow queue")
        if not self.public_base_url or urlparse(self.public_base_url).scheme != "https":
            raise ValueError(f"{self.environment} must define an HTTPS XIANYU_PUBLIC_BASE_URL")
        if not self.auth_enabled:
            raise ValueError(f"{self.environment} must enable reviewer authentication")
        if not self.should_use_secure_auth_cookie():
            raise ValueError(f"{self.environment} must use Secure reviewer session cookies")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
