from __future__ import annotations

import asyncio

import httpx
import pytest
from fakeredis import FakeRedis
from pydantic import ValidationError
from rq import Queue, Worker

from dispute_agent.api import create_app
from dispute_agent.config import Settings
from dispute_agent.workflow_queue import RQWorkflowQueue


class QueueWithoutWorkers:
    def enqueue(self, job: dict) -> str:
        return str(job["rq_job_id"])

    def cancel(self, rq_job_id: str | None) -> None:
        return None

    def health(self) -> dict:
        return {"status": "ok", "backend": "test"}

    def worker_health(self) -> dict:
        return {"status": "unavailable", "active_worker_count": 0, "workers": []}


class UnavailableQueue(QueueWithoutWorkers):
    def health(self) -> dict:
        return {"status": "unavailable", "backend": "test"}


def test_staging_and_production_profiles_enforce_postgres_rq_and_https() -> None:
    valid = Settings(
        _env_file=None,
        environment="staging",
        database_url="postgresql+psycopg://user:password@postgres/xianyu",
        workflow_queue_backend="rq",
        public_base_url="https://staging.example.com",
    )
    assert valid.should_require_worker_for_readiness() is True

    with pytest.raises(ValidationError, match="must use PostgreSQL"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="sqlite:///data/prod.db",
            public_base_url="https://disputes.example.com",
        )
    with pytest.raises(ValidationError, match="must define an HTTPS"):
        Settings(
            _env_file=None,
            environment="production",
            database_url="postgresql+psycopg://user:password@postgres/xianyu",
            public_base_url="http://disputes.example.com",
        )


def test_ready_distinguishes_liveness_from_missing_worker(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        settings = context.settings.model_copy(update={"readiness_require_worker": True})
        app = create_app(context.sessions, settings=settings, workflow_queue=QueueWithoutWorkers())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}

            ready = await client.get("/ready")
            assert ready.status_code == 503
            assert ready.json()["status"] == "not_ready"
            assert ready.json()["checks"]["database"]["status"] == "ok"
            assert ready.json()["checks"]["workers"]["active_worker_count"] == 0

            workers = await client.get("/worker/health")
            assert workers.status_code == 503

    asyncio.run(exercise())


def test_workflow_health_returns_service_unavailable_for_queue_outage(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        app = create_app(context.sessions, settings=context.settings, workflow_queue=UnavailableQueue())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/workflow/health")
            assert response.status_code == 503
            assert response.json()["status"] == "unavailable"

    asyncio.run(exercise())


def test_rq_worker_health_uses_live_registration(context) -> None:  # type: ignore[no-untyped-def]
    settings = context.settings.model_copy(
        update={"workflow_queue_backend": "rq", "workflow_queue_name": "deployment-health-test"}
    )
    connection = FakeRedis()
    queue = Queue(settings.workflow_queue_name, connection=connection)
    adapter = RQWorkflowQueue(settings)
    adapter.connection = connection
    adapter.queue = queue

    assert adapter.worker_health()["status"] == "unavailable"
    worker = Worker([queue], connection=connection, name="deployment-test-worker", worker_ttl=420)
    worker.register_birth()
    try:
        health = adapter.worker_health()
        assert health["status"] == "ok"
        assert health["active_worker_count"] == 1
        assert health["workers"][0]["name"] == "deployment-test-worker"
        assert health["workers"][0]["registration_ttl_seconds"] > 0
    finally:
        worker.register_death()
