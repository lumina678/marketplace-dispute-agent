from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from fakeredis import FakeRedis
from rq import Queue
from rq.job import Job

from dispute_agent.api import create_app
from dispute_agent.errors import QueueUnavailableError
from dispute_agent.models import WorkflowJob, utc_now
from dispute_agent.services.workflow_jobs import WorkflowJobService, WorkflowPauseRequested
from dispute_agent.workflow_queue import RQWorkflowQueue


class DeferredQueue:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.cancelled: list[str] = []

    def enqueue(self, job: dict) -> str:
        self.enqueued.append(job["rq_job_id"])
        return job["rq_job_id"]

    def cancel(self, rq_job_id: str | None) -> None:
        if rq_job_id:
            self.cancelled.append(rq_job_id)

    def health(self) -> dict:
        return {"status": "ok", "backend": "deferred-test"}


class UnavailableQueue(DeferredQueue):
    def enqueue(self, job: dict) -> str:
        raise QueueUnavailableError("test redis unavailable")


def control_payload(reason: str) -> dict[str, str]:
    return {"actor_id": "reviewer_test", "reason": reason}


def test_post_returns_durable_job_and_deduplicates_active_case(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        queue = DeferredQueue()
        app = create_app(context.sessions, settings=context.settings, workflow_queue=queue)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/cases/case_clear_mismatch/workflow", json={})
            assert first.status_code == 202
            first_job = first.json()
            assert first_job["status"] == "QUEUED"
            assert first_job["job_id"].startswith("wjob_")
            assert first_job["events_url"].endswith("/events")

            duplicate = await client.post("/cases/case_clear_mismatch/workflow", json={})
            assert duplicate.status_code == 202
            assert duplicate.json()["job_id"] == first_job["job_id"]
            assert duplicate.json()["deduplicated"] is True
            assert queue.enqueued == [first_job["rq_job_id"]]

            latest = await client.get("/cases/case_clear_mismatch/workflow-jobs/latest")
            assert latest.status_code == 200
            assert latest.json()["job_id"] == first_job["job_id"]

    asyncio.run(exercise())


def test_queued_job_can_pause_resume_and_cancel(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        queue = DeferredQueue()
        app = create_app(context.sessions, settings=context.settings, workflow_queue=queue)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = (await client.post("/cases/case_clear_mismatch/workflow", json={})).json()
            paused_response = await client.post(
                f"/workflow-jobs/{created['job_id']}/pause",
                json=control_payload("等待人工核对材料"),
            )
            assert paused_response.status_code == 202
            paused = paused_response.json()
            assert paused["status"] == "PAUSED"
            assert queue.cancelled == [created["rq_job_id"]]

            resumed_response = await client.post(
                f"/workflow-jobs/{created['job_id']}/resume",
                json=control_payload("材料核对完成"),
            )
            assert resumed_response.status_code == 202
            resumed = resumed_response.json()
            assert resumed["status"] == "QUEUED"
            assert resumed["delivery_version"] == 2
            assert resumed["rq_job_id"] != created["rq_job_id"]

            cancelled_response = await client.post(
                f"/workflow-jobs/{created['job_id']}/cancel",
                json=control_payload("审核员终止本次调查"),
            )
            assert cancelled_response.status_code == 202
            cancelled = cancelled_response.json()
            assert cancelled["status"] == "CANCELLED"
            assert cancelled["terminal"] is True

            replacement = await client.post("/cases/case_clear_mismatch/workflow", json={})
            assert replacement.status_code == 202
            assert replacement.json()["job_id"] != created["job_id"]

    asyncio.run(exercise())


def test_running_job_pause_is_cooperative_at_safe_boundary(context) -> None:  # type: ignore[no-untyped-def]
    jobs = WorkflowJobService(context.sessions, settings=context.settings)
    job, created = jobs.create_or_get_active("case_clear_mismatch", actor_id="test")
    assert created is True
    running = jobs.begin_attempt(job["job_id"], worker_id="worker-test")
    assert running["status"] == "RUNNING"

    requested = jobs.request_pause(
        job["job_id"],
        actor_id="reviewer_test",
        reason="人工复核证据来源",
    )
    assert requested["status"] == "PAUSE_REQUESTED"
    with pytest.raises(WorkflowPauseRequested):
        jobs.check_control(job["job_id"])
    paused = jobs.confirm_paused(job["job_id"], "人工复核证据来源")
    assert paused["status"] == "PAUSED"


def test_inline_execution_persists_agent_stages_and_sse_events(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        app = create_app(context.sessions, settings=context.settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/cases/case_clear_mismatch/workflow", json={})
            assert response.status_code == 202
            job = response.json()
            assert job["status"] == "COMPLETED", job["error"]
            assert job["result"]["workflow_boundary"] == "HUMAN_REVIEW_REQUIRED"
            assert {item["agent_role"] for item in job["stages"]} == {
                "BUYER_CASE_ANALYST",
                "SELLER_CASE_ANALYST",
                "EVIDENCE_POLICY_CLERK",
                "EVIDENCE_GAP_PLANNER",
                "ADJUDICATION_AGENT",
                "DECISION_GUARD",
            }
            assert all(item["status"] == "COMPLETED" for item in job["stages"])

            events = await client.get(job["events_url"])
            assert events.status_code == 200
            assert events.headers["content-type"].startswith("text/event-stream")
            assert "JOB_QUEUED" in events.text
            assert "STAGE_STARTED" in events.text
            assert "JOB_COMPLETED" in events.text
            ids = [
                int(line.removeprefix("id: "))
                for line in events.text.splitlines()
                if line.startswith("id: ")
            ]
            assert ids == list(range(1, job["event_sequence"] + 1))

            resumed_stream = await client.get(
                job["events_url"],
                headers={"Last-Event-ID": str(job["event_sequence"] - 1)},
            )
            resumed_ids = [
                int(line.removeprefix("id: "))
                for line in resumed_stream.text.splitlines()
                if line.startswith("id: ")
            ]
            assert resumed_ids == [job["event_sequence"]]

    asyncio.run(exercise())


def test_queue_outage_returns_503_but_keeps_retryable_outbox_job(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        app = create_app(
            context.sessions,
            settings=context.settings,
            workflow_queue=UnavailableQueue(),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/cases/case_clear_mismatch/workflow", json={})
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "QUEUE_UNAVAILABLE"
            latest = (await client.get("/cases/case_clear_mismatch/workflow-jobs/latest")).json()
            assert latest["status"] == "QUEUED"
            assert latest["error"]["code"] == "QUEUE_UNAVAILABLE"
            pending = app.state.workflow_jobs.pending_dispatches()
            assert [item["job_id"] for item in pending] == [latest["job_id"]]

    asyncio.run(exercise())


def test_stale_running_job_is_recovered_with_new_delivery(context) -> None:  # type: ignore[no-untyped-def]
    jobs = WorkflowJobService(context.sessions, settings=context.settings)
    job, _created = jobs.create_or_get_active("case_clear_mismatch", actor_id="test")
    running = jobs.begin_attempt(job["job_id"], worker_id="dead-worker")
    with context.sessions() as session:
        persisted = session.get(WorkflowJob, running["job_id"])
        assert persisted is not None
        persisted.heartbeat_at = utc_now() - timedelta(
            seconds=context.settings.workflow_stale_after_seconds + 1
        )
        session.commit()

    recovered = jobs.recover_stale()
    assert len(recovered) == 1
    assert recovered[0]["status"] == "RETRYING"
    assert recovered[0]["delivery_version"] == 2
    assert recovered[0]["rq_job_id"] != running["rq_job_id"]


def test_rq_adapter_enqueues_retryable_delivery_with_real_rq_types(context) -> None:  # type: ignore[no-untyped-def]
    settings = context.settings.model_copy(
        update={
            "workflow_queue_backend": "rq",
            "workflow_queue_name": "xianyu-test",
            "workflow_max_attempts": 3,
            "workflow_retry_delays_seconds": "1,2",
        }
    )
    adapter = RQWorkflowQueue(settings)
    adapter.connection = FakeRedis()
    adapter.queue = Queue(settings.workflow_queue_name, connection=adapter.connection)
    payload = {
        "job_id": "wjob_adapter_test",
        "case_id": "case_clear_mismatch",
        "rq_job_id": "xianyu-wjob_adapter_test-v1",
        "max_attempts": 3,
        "timeout_seconds": 120,
    }

    assert adapter.enqueue(payload) == payload["rq_job_id"]
    queued = Job.fetch(payload["rq_job_id"], connection=adapter.connection)
    assert queued.func_name == "dispute_agent.worker.perform_workflow_job"
    assert queued.timeout == 120
    assert queued.retries_left == 2
    assert adapter.health()["status"] == "ok"
    adapter.cancel(payload["rq_job_id"])
    assert getattr(queued.get_status(refresh=True), "value", queued.get_status()) == "canceled"
