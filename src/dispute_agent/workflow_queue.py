from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue, Retry, Worker
from rq.job import Job
from rq.exceptions import NoSuchJobError

from dispute_agent.config import Settings
from dispute_agent.errors import QueueUnavailableError
from dispute_agent.services.workflow_jobs import WorkflowJobExecutor, WorkflowJobService


class WorkflowQueue(Protocol):
    def enqueue(self, job: dict) -> str:
        ...

    def cancel(self, rq_job_id: str | None) -> None:
        ...

    def health(self) -> dict:
        ...

    def worker_health(self) -> dict:
        ...


class RQWorkflowQueue:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.connection = Redis.from_url(settings.redis_url)
        self.queue = Queue(settings.workflow_queue_name, connection=self.connection)

    def enqueue(self, job: dict) -> str:
        rq_job_id = str(job["rq_job_id"])
        try:
            try:
                existing = Job.fetch(rq_job_id, connection=self.connection)
            except NoSuchJobError:
                existing = None
            if existing is not None:
                return existing.id

            retry_count = max(0, int(job["max_attempts"]) - 1)
            retry = None
            if retry_count:
                configured = self.settings.workflow_retry_delays()
                delays = configured or [0]
                delays = (delays + [delays[-1]] * retry_count)[:retry_count]
                retry = Retry(max=retry_count, interval=delays)
            queued = self.queue.enqueue_call(
                func="dispute_agent.worker.perform_workflow_job",
                args=(job["job_id"],),
                job_id=rq_job_id,
                retry=retry,
                timeout=int(job["timeout_seconds"]),
                result_ttl=86_400,
                failure_ttl=604_800,
                description=f"case={job['case_id']} workflow_job={job['job_id']}",
            )
            return queued.id
        except (RedisError, OSError, ValueError) as exc:
            raise QueueUnavailableError(f"Redis/RQ 投递失败: {exc}") from exc

    def cancel(self, rq_job_id: str | None) -> None:
        if not rq_job_id:
            return
        try:
            job = Job.fetch(rq_job_id, connection=self.connection)
            status = job.get_status(refresh=True)
            status_value = getattr(status, "value", status)
            if status_value in {"queued", "deferred", "scheduled"}:
                job.cancel()
        except NoSuchJobError:
            return
        except (RedisError, OSError) as exc:
            raise QueueUnavailableError(f"Redis/RQ 取消投递失败: {exc}") from exc

    def health(self) -> dict:
        try:
            ping = bool(self.connection.ping())
            return {
                "status": "ok" if ping else "unavailable",
                "backend": "rq",
                "queue": self.settings.workflow_queue_name,
                "redis": "reachable" if ping else "unreachable",
                "queued_jobs": self.queue.count,
            }
        except (RedisError, OSError) as exc:
            return {
                "status": "unavailable",
                "backend": "rq",
                "queue": self.settings.workflow_queue_name,
                "redis": "unreachable",
                "error": str(exc)[:500],
            }

    def worker_health(self) -> dict:
        try:
            workers = Worker.all(connection=self.connection, queue=self.queue)
            active_workers: list[dict] = []
            for worker in workers:
                ttl = int(self.connection.ttl(worker.key))
                last_heartbeat = worker.last_heartbeat
                heartbeat_age = (
                    (datetime.now(timezone.utc) - last_heartbeat).total_seconds()
                    if last_heartbeat is not None
                    else float("inf")
                )
                # RQ refreshes the Redis key expiry as its authoritative liveness
                # signal. Idle workers may not update the timestamp frequently,
                # so heartbeat age is diagnostic only and TTL decides health.
                if ttl <= 0:
                    continue
                state = worker.get_state()
                active_workers.append(
                    {
                        "name": worker.name,
                        "state": getattr(state, "value", state),
                        "heartbeat_age_seconds": round(heartbeat_age, 3),
                        "registration_ttl_seconds": ttl,
                    }
                )
            return {
                "status": "ok" if active_workers else "unavailable",
                "backend": "rq",
                "queue": self.settings.workflow_queue_name,
                "active_worker_count": len(active_workers),
                "workers": active_workers,
            }
        except (RedisError, OSError, ValueError) as exc:
            return {
                "status": "unavailable",
                "backend": "rq",
                "queue": self.settings.workflow_queue_name,
                "active_worker_count": 0,
                "error": type(exc).__name__,
            }


class InlineWorkflowQueue:
    """Explicit test adapter. Never selected by the production default."""

    def __init__(self, executor: WorkflowJobExecutor, jobs: WorkflowJobService):
        self.executor = executor
        self.jobs = jobs

    def enqueue(self, job: dict) -> str:
        while True:
            try:
                self.executor.execute(job["job_id"], worker_id="inline-test-worker")
            except Exception:
                current = self.jobs.get(job["job_id"])
                if current["status"] == "RETRYING":
                    continue
            break
        return str(job["rq_job_id"])

    def cancel(self, rq_job_id: str | None) -> None:
        return None

    def health(self) -> dict:
        return {"status": "ok", "backend": "inline", "queue": "inline-test-only"}

    def worker_health(self) -> dict:
        return {
            "status": "ok",
            "backend": "inline",
            "active_worker_count": 1,
            "workers": [{"name": "inline-test-worker", "state": "idle"}],
        }


def create_workflow_queue(
    settings: Settings,
    *,
    executor: WorkflowJobExecutor,
    jobs: WorkflowJobService,
) -> WorkflowQueue:
    backend = settings.workflow_queue_backend.strip().lower()
    if backend == "rq":
        return RQWorkflowQueue(settings)
    if backend == "inline":
        return InlineWorkflowQueue(executor, jobs)
    raise ValueError(f"Unsupported workflow queue backend: {settings.workflow_queue_backend}")
