from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.config import Settings
from dispute_agent.workflow_queue import WorkflowQueue


class DeploymentHealthService:
    """Dependency health used by readiness endpoints and container probes."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        queue: WorkflowQueue,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.queue = queue
        self.settings = settings

    def database(self) -> dict[str, Any]:
        try:
            with self.session_factory() as session:
                session.execute(text("SELECT 1"))
            return {"status": "ok"}
        except Exception as exc:  # readiness reports dependency failure instead of raising 500
            return {"status": "unavailable", "error": type(exc).__name__}

    def workers(self) -> dict[str, Any]:
        worker_probe = getattr(self.queue, "worker_health", None)
        if worker_probe is None:
            return {
                "status": "not_checked",
                "active_worker_count": None,
                "reason": "queue adapter does not expose worker health",
            }
        return worker_probe()

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        database = self.database()
        queue = self.queue.health()
        workers = self.workers()
        require_worker = self.settings.should_require_worker_for_readiness()
        ready = database.get("status") == "ok" and queue.get("status") == "ok"
        if require_worker:
            ready = ready and workers.get("status") == "ok"
        return ready, {
            "status": "ready" if ready else "not_ready",
            "service": "xianyu-api",
            "environment": self.settings.environment,
            "release": self.settings.release,
            "checks": {
                "database": database,
                "queue": queue,
                "workers": workers,
            },
            "worker_required": require_worker,
        }
