from __future__ import annotations

import os
import socket

from rq import Worker, get_current_job

from dispute_agent.agents.backend import create_generation_backend
from dispute_agent.agents.runtime import AgentRuntime
from dispute_agent.agents.workflow import InvestigationWorkflow
from dispute_agent.config import get_settings
from dispute_agent.db import SessionLocal
from dispute_agent.services.claim_router import ClaimRoutingService
from dispute_agent.services.decision_guard import DecisionGuard
from dispute_agent.services.orchestrator import CaseOrchestrator
from dispute_agent.services.tools import ToolService
from dispute_agent.services.workflow_jobs import WorkflowJobExecutor, WorkflowJobService
from dispute_agent.skills import get_skill_registry
from dispute_agent.workflow_queue import RQWorkflowQueue


def build_executor() -> WorkflowJobExecutor:
    settings = get_settings()
    skills = get_skill_registry()
    tools = ToolService(SessionLocal)
    routing = ClaimRoutingService(SessionLocal, settings=settings, skill_registry=skills)
    orchestrator = CaseOrchestrator(SessionLocal, settings=settings, routing_service=routing)
    runtime = AgentRuntime(
        SessionLocal,
        backend=create_generation_backend(settings),
        tools=tools,
        skill_registry=skills,
    )
    workflow = InvestigationWorkflow(
        SessionLocal,
        orchestrator=orchestrator,
        runtime=runtime,
        tools=tools,
        decision_guard=DecisionGuard(SessionLocal),
    )
    jobs = WorkflowJobService(SessionLocal, settings=settings)
    return WorkflowJobExecutor(jobs, workflow, settings=settings)


def perform_workflow_job(job_id: str) -> dict:
    rq_job = get_current_job()
    delivery_id = rq_job.id if rq_job else "direct"
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{delivery_id}"
    return build_executor().execute(job_id, worker_id=worker_id)


def main() -> None:
    settings = get_settings()
    if settings.workflow_queue_backend.strip().lower() != "rq":
        raise RuntimeError("xianyu-worker requires XIANYU_WORKFLOW_QUEUE_BACKEND=rq")
    queue = RQWorkflowQueue(settings)
    jobs = WorkflowJobService(SessionLocal, settings=settings)
    jobs.recover_stale()
    for pending in jobs.pending_dispatches():
        queue.enqueue(pending)
    worker_name = f"xianyu-{socket.gethostname()}-{os.getpid()}"
    Worker(
        [settings.workflow_queue_name],
        connection=queue.connection,
        name=worker_name,
        worker_ttl=settings.worker_ttl_seconds,
    ).work(with_scheduler=True)


if __name__ == "__main__":
    main()
