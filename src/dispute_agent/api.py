from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.db import SessionLocal
from dispute_agent.errors import DisputeAgentError, NotFoundError, QueueUnavailableError
from dispute_agent.agents.backend import StructuredGenerationBackend, create_generation_backend
from dispute_agent.agents.runtime import AgentRuntime
from dispute_agent.agents.workflow import InvestigationWorkflow
from dispute_agent.models import CaseEvent, Dispute
from dispute_agent.serialization import jsonable
from dispute_agent.services.orchestrator import CaseOrchestrator
from dispute_agent.services.evidence_submission import EvidenceSubmissionService
from dispute_agent.services.decision_guard import DecisionGuard
from dispute_agent.services.demo_cases import DemoCaseService
from dispute_agent.services.human_review import HumanReviewService
from dispute_agent.services.execution import ResolutionExecutionService
from dispute_agent.services.appeals import AppealService
from dispute_agent.services.evaluation import EvaluationService
from dispute_agent.services.tools import ToolService
from dispute_agent.services.workbench import WorkbenchService
from dispute_agent.services.claim_router import ClaimRoutingService
from dispute_agent.services.intake import CaseIntakeService
from dispute_agent.services.workflow_jobs import TERMINAL_JOB_STATUSES, WorkflowJobExecutor, WorkflowJobService
from dispute_agent.services.deployment_health import DeploymentHealthService
from dispute_agent.workflow_queue import WorkflowQueue, create_workflow_queue
from dispute_agent.intake_schemas import (
    ChatBatchImportRequest,
    DisputeSubmitRequest,
    FreezeCaseMaterialsRequest,
    ListingSnapshotImportRequest,
    TextEvidenceUploadRequest,
    TransactionCreateRequest,
)
from dispute_agent.routing_schemas import (
    ClaimRoutingHint,
    RouteCaseRequest,
    RouteClaimRequest,
    RoutingOverrideRequest,
)
from dispute_agent.config import Settings, get_settings
from dispute_agent.dispute_types import DisputeType, RoutingSource, RoutingStatus
from dispute_agent.skills import get_skill_registry


class PhaseResultRequest(BaseModel):
    actor: str = Field(min_length=1)
    payload: dict[str, Any]
    tokens_used: int = Field(default=0, ge=0)


class ResumeEvidenceRequest(BaseModel):
    target: str
    evidence_id: str
    question_ids: list[str] = Field(min_length=1)
    actor_id: str = "orchestrator"


class ToolCallRequest(BaseModel):
    actor: str
    parameters: dict[str, Any]
    case_run_id: str | None = None


class WorkflowRequest(BaseModel):
    actor_id: str = "investigation-workflow"


class WorkflowControlRequest(BaseModel):
    actor_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=1000)


class EvidenceAndResumeRequest(BaseModel):
    target: str
    question_ids: list[str] = Field(min_length=1)
    evidence_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    captured_at: datetime
    extracted_facts: list[dict[str, Any]] = Field(default_factory=list)
    related_claim_ids: list[str] | None = None
    content_sha256: str | None = None
    immutable_uri: str | None = None
    source_system: str = "EVIDENCE_STORE"
    handling_flags: list[str] = Field(default_factory=list)
    actor_id: str = "orchestrator"
    auto_continue: bool = True


class GuardRequest(BaseModel):
    case_run_id: str
    decision_id: str | None = None


class ReviewDecisionRequest(BaseModel):
    decision_id: str
    reviewer_id: str
    reason: str = Field(min_length=1)


class ReturnToInvestigationRequest(BaseModel):
    decision_id: str
    reviewer_id: str
    instructions: str = Field(min_length=1)
    claim_ids: list[str] | None = None


class ExecutionRequest(BaseModel):
    decision_id: str
    actor_id: str = "mock-executor"


class AppealEvidenceRequest(BaseModel):
    evidence_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source_record_id: str = Field(min_length=1)
    captured_at: datetime
    related_claim_ids: list[str] = Field(min_length=1)
    extracted_facts: list[dict[str, Any]] = Field(default_factory=list)
    content_sha256: str | None = None
    immutable_uri: str | None = None
    source_system: str = "APPEAL_EVIDENCE_STORE"
    handling_flags: list[str] = Field(default_factory=list)


class AppealSubmitRequest(BaseModel):
    appellant_id: str
    appellant_role: str
    grounds: str
    statement: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    new_evidence: list[AppealEvidenceRequest] = Field(default_factory=list)


class AppealReviewRequest(BaseModel):
    reviewer_id: str
    reason: str = Field(min_length=1)


def create_app(
    session_factory: sessionmaker[Session] = SessionLocal,
    *,
    settings: Settings | None = None,
    generation_backend: StructuredGenerationBackend | None = None,
    workflow_queue: WorkflowQueue | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    backend = generation_backend or create_generation_backend(settings)
    app = FastAPI(
        title="Marketplace Dispute Agent",
        version="0.1.0",
        description="Multi-agent investigation and adjudication support for second-hand marketplace disputes.",
    )
    skill_registry = get_skill_registry()
    tool_service = ToolService(session_factory)
    routing = ClaimRoutingService(session_factory, settings=settings, skill_registry=skill_registry)
    orchestrator = CaseOrchestrator(session_factory, settings=settings, routing_service=routing)
    runtime = AgentRuntime(session_factory, backend=backend, tools=tool_service)
    decision_guard = DecisionGuard(session_factory)
    workflow = InvestigationWorkflow(
        session_factory,
        orchestrator=orchestrator,
        runtime=runtime,
        tools=tool_service,
        decision_guard=decision_guard,
    )
    workflow_jobs = WorkflowJobService(session_factory, settings=settings)
    workflow_executor = WorkflowJobExecutor(workflow_jobs, workflow, settings=settings)
    task_queue = workflow_queue or create_workflow_queue(
        settings,
        executor=workflow_executor,
        jobs=workflow_jobs,
    )
    evidence_submission = EvidenceSubmissionService(session_factory, orchestrator=orchestrator)
    human_review = HumanReviewService(
        session_factory,
        settings=orchestrator.settings,
        state_machine=orchestrator.state_machine,
    )
    execution = ResolutionExecutionService(
        session_factory,
        settings=orchestrator.settings,
        state_machine=orchestrator.state_machine,
        tools=tool_service,
    )
    appeals = AppealService(
        session_factory,
        settings=orchestrator.settings,
        state_machine=orchestrator.state_machine,
    )
    evaluation = EvaluationService(session_factory)
    workbench = WorkbenchService(session_factory)
    demo_cases = DemoCaseService(session_factory)
    intake = CaseIntakeService(
        session_factory,
        routing_service=routing,
        state_machine=orchestrator.state_machine,
        skill_registry=skill_registry,
    )
    app.state.generation_backend = backend
    app.state.skill_registry = skill_registry
    app.state.claim_router = routing
    app.state.workflow_jobs = workflow_jobs
    app.state.workflow_executor = workflow_executor
    app.state.workflow_queue = task_queue
    deployment_health = DeploymentHealthService(session_factory, task_queue, settings)

    def get_session() -> Session:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    @app.exception_handler(DisputeAgentError)
    async def dispute_error_handler(_request, exc: DisputeAgentError):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        status = (
            404
            if exc.code == "NOT_FOUND"
            else 403
            if exc.code == "NOT_AUTHORIZED"
            else 409
            if exc.code in {"CONFLICT", "INVALID_STATE_TRANSITION"}
            else 502
            if exc.code == "MODEL_BACKEND_ERROR"
            else 503
            if exc.code == "QUEUE_UNAVAILABLE"
            else 400
        )
        return JSONResponse(status_code=status, content={"error": {"code": exc.code, "message": str(exc)}})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    def ready() -> JSONResponse:
        is_ready, payload = deployment_health.readiness()
        return JSONResponse(status_code=200 if is_ready else 503, content=payload)

    @app.get("/worker/health")
    def worker_health() -> JSONResponse:
        payload = deployment_health.workers()
        healthy = payload.get("status") == "ok"
        return JSONResponse(status_code=200 if healthy else 503, content=payload)

    @app.get("/model")
    def model_configuration() -> dict[str, Any]:
        return backend.describe()

    @app.get("/model/health")
    def model_health(probe: bool = Query(default=True)) -> dict[str, Any]:
        return backend.health_check(probe=probe)

    @app.get("/workflow/health")
    def workflow_health() -> JSONResponse:
        payload = task_queue.health()
        healthy = payload.get("status") == "ok"
        return JSONResponse(status_code=200 if healthy else 503, content=payload)

    @app.get("/dispute-types")
    def dispute_types() -> dict[str, Any]:
        return {
            "dispute_types": [item.value for item in DisputeType],
            "routing_sources": [item.value for item in RoutingSource],
            "routing_statuses": [item.value for item in RoutingStatus],
        }

    @app.get("/skills")
    def list_skills(dispute_type: str | None = Query(default=None)) -> list[dict[str, Any]]:
        manifests = (
            [item.manifest for item in skill_registry.for_dispute_type(dispute_type)]
            if dispute_type
            else list(skill_registry.manifests())
        )
        return [item.model_dump(mode="json") for item in manifests]

    @app.get("/skills/{skill_name}")
    def get_skill(skill_name: str, version: str | None = Query(default=None)) -> dict[str, Any]:
        return skill_registry.get(skill_name, version).manifest.model_dump(mode="json")

    @app.post("/transactions")
    def create_transaction(request: TransactionCreateRequest) -> dict[str, Any]:
        return intake.create_transaction(request)

    @app.post("/transactions/{transaction_id}/listing-snapshots")
    def import_listing_snapshot(
        transaction_id: str,
        request: ListingSnapshotImportRequest,
    ) -> dict[str, Any]:
        return intake.import_listing_snapshot(transaction_id, request)

    @app.post("/transactions/{transaction_id}/messages:batch")
    def import_chat_batch(
        transaction_id: str,
        request: ChatBatchImportRequest,
    ) -> dict[str, Any]:
        return intake.import_chat_batch(transaction_id, request)

    @app.post("/disputes")
    def submit_dispute(request: DisputeSubmitRequest) -> dict[str, Any]:
        return intake.submit_dispute(request)

    @app.get("/cases/{case_id}/routing")
    def get_case_routing(case_id: str) -> dict[str, Any]:
        return jsonable(routing.get_case_routing(case_id))

    @app.post("/cases/{case_id}/routing")
    def route_case(case_id: str, request: RouteCaseRequest) -> dict[str, Any]:
        hints = {
            item.claim_id: ClaimRoutingHint.model_validate(item.model_dump(exclude={"claim_id"}))
            for item in request.claim_hints
        }
        return jsonable(
            routing.route_case(
                case_id,
                hints=hints,
                actor_id=request.actor_id,
                force_recompute=request.force_recompute,
            )
        )

    @app.post("/cases/{case_id}/claims/{claim_id}/routing")
    def route_claim(case_id: str, claim_id: str, request: RouteClaimRequest) -> dict[str, Any]:
        hint = ClaimRoutingHint.model_validate(request.model_dump(exclude={"actor_id", "force_recompute"}))
        return jsonable(
            routing.route_claim(
                case_id,
                claim_id,
                hint=hint,
                actor_id=request.actor_id,
                force_recompute=request.force_recompute,
            )
        )

    @app.post("/cases/{case_id}/claims/{claim_id}/routing-override")
    def override_claim_routing(case_id: str, claim_id: str, request: RoutingOverrideRequest) -> dict[str, Any]:
        return jsonable(
            routing.override_claim(
                case_id,
                claim_id,
                reviewer_id=request.reviewer_id,
                issue_type=request.issue_type,
                claim_type=request.claim_type,
                reason=request.reason,
            )
        )

    @app.get("/cases")
    def list_cases(
        state: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(get_session),
    ) -> list[dict[str, Any]]:
        statement = select(Dispute).order_by(Dispute.created_at).limit(limit)
        if state:
            statement = statement.where(Dispute.state == state)
        return [
            jsonable(
                {
                    "case_id": item.id,
                    "transaction_id": item.transaction_id,
                    "dispute_type": item.dispute_type,
                    "state": item.state,
                    "state_version": item.state_version,
                    "policy_id": item.policy_id,
                    "policy_version": item.policy_version,
                    "created_at": item.created_at,
                }
            )
            for item in session.scalars(statement)
        ]

    @app.get("/cases/{case_id}")
    def get_case(case_id: str) -> dict[str, Any]:
        return tool_service.call("case.get_state", {"case_id": case_id}, actor="REVIEWER")

    @app.get("/cases/{case_id}/intake")
    def get_case_intake(case_id: str) -> dict[str, Any]:
        return intake.get_intake(case_id)

    @app.post("/cases/{case_id}/evidence")
    def upload_text_evidence(
        case_id: str,
        request: TextEvidenceUploadRequest,
    ) -> dict[str, Any]:
        return intake.upload_text_evidence(case_id, request)

    @app.post("/cases/{case_id}/freeze")
    def freeze_case_materials(
        case_id: str,
        request: FreezeCaseMaterialsRequest,
    ) -> dict[str, Any]:
        return intake.freeze_case_materials(case_id, request)

    @app.get("/cases/{case_id}/events")
    def get_events(case_id: str, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
        if session.get(Dispute, case_id) is None:
            raise HTTPException(status_code=404, detail="case not found")
        events = session.scalars(
            select(CaseEvent).where(CaseEvent.dispute_id == case_id).order_by(CaseEvent.sequence)
        )
        return [
            jsonable(
                {
                    "case_event_id": event.id,
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "transition_id": event.transition_id,
                    "from_state": event.from_state,
                    "to_state": event.to_state,
                    "actor_type": event.actor_type,
                    "actor_id": event.actor_id,
                    "reason_code": event.reason_code,
                    "expected_state_version": event.expected_state_version,
                    "new_state_version": event.new_state_version,
                    "metadata": event.metadata_json,
                    "occurred_at": event.occurred_at,
                }
            )
            for event in events
        ]

    @app.get("/cases/{case_id}/workbench")
    def get_workbench(case_id: str) -> dict[str, Any]:
        result = workbench.get(case_id)
        result["workflow_job"] = workflow_jobs.latest_for_case(case_id)
        return result

    @app.post("/cases/{case_id}/demo-reset")
    def reset_text_demo(case_id: str) -> dict[str, Any]:
        return demo_cases.reset_text_demo(case_id)

    @app.get("/evaluation")
    def evaluate_cases(case_id: list[str] | None = Query(default=None)) -> dict[str, Any]:
        return evaluation.evaluate(case_ids=case_id)

    @app.get("/workbench", response_class=HTMLResponse)
    def workbench_page() -> str:
        page = settings.web_directory / "workbench.html"
        return page.read_text(encoding="utf-8")

    @app.post("/cases/{case_id}/runs")
    def start_run(case_id: str) -> dict[str, Any]:
        return orchestrator.start(case_id)

    def dispatch_workflow_job(job: dict[str, Any]) -> dict[str, Any]:
        if job["status"] in {"QUEUED", "RETRYING"}:
            try:
                task_queue.enqueue(job)
            except QueueUnavailableError as exc:
                workflow_jobs.record_dispatch_error(job["job_id"], exc)
                raise
            return workflow_jobs.record_dispatched(job["job_id"])
        return workflow_jobs.get(job["job_id"])

    @app.post("/cases/{case_id}/workflow", status_code=status.HTTP_202_ACCEPTED)
    def run_workflow(case_id: str, request: WorkflowRequest | None = None) -> dict[str, Any]:
        job, created = workflow_jobs.create_or_get_active(
            case_id,
            actor_id=request.actor_id if request else "investigation-workflow",
        )
        if created or (job.get("error") or {}).get("code") == "QUEUE_UNAVAILABLE":
            return dispatch_workflow_job(job)
        return job

    @app.get("/workflow-jobs/{job_id}")
    def get_workflow_job(job_id: str) -> dict[str, Any]:
        return workflow_jobs.get(job_id)

    @app.get("/cases/{case_id}/workflow-jobs/latest")
    def get_latest_workflow_job(case_id: str) -> dict[str, Any]:
        job = workflow_jobs.latest_for_case(case_id)
        if job is None:
            raise NotFoundError(f"案件尚无 Workflow Job: {case_id}")
        return job

    @app.get("/workflow-jobs/{job_id}/events")
    async def stream_workflow_events(job_id: str, request: Request) -> StreamingResponse:
        workflow_jobs.get(job_id)
        header_sequence = request.headers.get("last-event-id", "0")
        query_sequence = request.query_params.get("after", "0")
        try:
            cursor = max(int(header_sequence), int(query_sequence), 0)
        except ValueError:
            cursor = 0

        async def event_stream():  # type: ignore[no-untyped-def]
            nonlocal cursor
            idle_ticks = 0
            while True:
                if await request.is_disconnected():
                    return
                events = workflow_jobs.events_after(job_id, cursor)
                for event in events:
                    cursor = event["sequence"]
                    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {cursor}\ndata: {payload}\n\n"
                job = workflow_jobs.get(job_id)
                if job["status"] in TERMINAL_JOB_STATUSES | {"PAUSED"} and cursor >= job["event_sequence"]:
                    return
                idle_ticks += 1
                if not events and idle_ticks % max(
                    1,
                    int(10 / settings.workflow_sse_poll_interval_seconds),
                ) == 0:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(settings.workflow_sse_poll_interval_seconds)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/workflow-jobs/{job_id}/pause", status_code=status.HTTP_202_ACCEPTED)
    def pause_workflow_job(job_id: str, request: WorkflowControlRequest) -> dict[str, Any]:
        before = workflow_jobs.get(job_id)
        job = workflow_jobs.request_pause(
            job_id,
            actor_id=request.actor_id,
            reason=request.reason,
        )
        if before["status"] in {"QUEUED", "RETRYING"}:
            task_queue.cancel(before["rq_job_id"])
        return job

    @app.post("/workflow-jobs/{job_id}/resume", status_code=status.HTTP_202_ACCEPTED)
    def resume_workflow_job(job_id: str, request: WorkflowControlRequest) -> dict[str, Any]:
        job = workflow_jobs.resume(
            job_id,
            actor_id=request.actor_id,
            reason=request.reason,
        )
        return dispatch_workflow_job(job)

    @app.post("/workflow-jobs/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    def cancel_workflow_job(job_id: str, request: WorkflowControlRequest) -> dict[str, Any]:
        before = workflow_jobs.get(job_id)
        job = workflow_jobs.request_cancel(
            job_id,
            actor_id=request.actor_id,
            reason=request.reason,
        )
        if before["status"] in {"QUEUED", "RETRYING", "PAUSED"}:
            task_queue.cancel(before["rq_job_id"])
        return job

    @app.get("/cases/{case_id}/agent-outputs")
    def list_agent_outputs(case_id: str, case_run_id: str | None = Query(default=None)) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in runtime.list_outputs(case_id, case_run_id)]

    @app.get("/cases/{case_id}/agent-outputs/{output_id}")
    def get_agent_output(case_id: str, output_id: str) -> dict[str, Any]:
        output = runtime.get_output(output_id)
        if output.case_id != case_id:
            raise NotFoundError(f"Agent 输出不属于案件: {case_id}")
        return output.model_dump(mode="json")

    @app.post("/cases/{case_id}/guard")
    def run_guard(case_id: str, request: GuardRequest) -> dict[str, Any]:
        return decision_guard.evaluate(
            case_id,
            request.case_run_id,
            decision_id=request.decision_id,
        ).model_dump(mode="json")

    @app.get("/cases/{case_id}/guard-results")
    def list_guard_results(case_id: str) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in decision_guard.list_for_case(case_id)]

    @app.get("/cases/{case_id}/review-package")
    def get_review_package(
        case_id: str,
        decision_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        return human_review.get_review_package(case_id, decision_id=decision_id)

    @app.post("/cases/{case_id}/reviews/approve")
    def approve_review(case_id: str, request: ReviewDecisionRequest) -> dict[str, Any]:
        return human_review.approve(
            case_id,
            decision_id=request.decision_id,
            reviewer_id=request.reviewer_id,
            reason=request.reason,
        )

    @app.post("/cases/{case_id}/reviews/reject")
    def reject_review(case_id: str, request: ReviewDecisionRequest) -> dict[str, Any]:
        return human_review.reject(
            case_id,
            decision_id=request.decision_id,
            reviewer_id=request.reviewer_id,
            reason=request.reason,
        )

    @app.post("/cases/{case_id}/reviews/return-to-investigation")
    def return_review_to_investigation(
        case_id: str,
        request: ReturnToInvestigationRequest,
    ) -> dict[str, Any]:
        return human_review.return_to_investigation(
            case_id,
            decision_id=request.decision_id,
            reviewer_id=request.reviewer_id,
            instructions=request.instructions,
            claim_ids=request.claim_ids,
        )

    @app.post("/cases/{case_id}/execution")
    def execute_resolution(case_id: str, request: ExecutionRequest) -> dict[str, Any]:
        return execution.execute(
            case_id,
            decision_id=request.decision_id,
            actor_id=request.actor_id,
        )

    @app.post("/cases/{case_id}/execution/retry")
    def retry_resolution(case_id: str, request: ExecutionRequest) -> dict[str, Any]:
        return execution.retry(
            case_id,
            decision_id=request.decision_id,
            actor_id=request.actor_id,
        )

    @app.get("/cases/{case_id}/execution")
    def get_execution_status(
        case_id: str,
        decision_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        return execution.get_status(case_id, decision_id=decision_id)

    @app.post("/cases/{case_id}/appeals")
    def submit_appeal(case_id: str, request: AppealSubmitRequest) -> dict[str, Any]:
        return appeals.submit(
            case_id,
            appellant_id=request.appellant_id,
            appellant_role=request.appellant_role,
            grounds=request.grounds,
            statement=request.statement,
            evidence_ids=request.evidence_ids,
            new_evidence=[item.model_dump() for item in request.new_evidence],
        )

    @app.get("/cases/{case_id}/appeals")
    def list_appeals(case_id: str) -> list[dict[str, Any]]:
        return appeals.list_for_case(case_id)

    @app.post("/cases/{case_id}/appeals/{appeal_id}/accept")
    def accept_appeal(
        case_id: str,
        appeal_id: str,
        request: AppealReviewRequest,
    ) -> dict[str, Any]:
        return appeals.accept(
            case_id,
            appeal_id=appeal_id,
            reviewer_id=request.reviewer_id,
            reason=request.reason,
        )

    @app.post("/cases/{case_id}/appeals/{appeal_id}/deny")
    def deny_appeal(
        case_id: str,
        appeal_id: str,
        request: AppealReviewRequest,
    ) -> dict[str, Any]:
        return appeals.deny(
            case_id,
            appeal_id=appeal_id,
            reviewer_id=request.reviewer_id,
            reason=request.reason,
        )

    @app.post("/cases/{case_id}/appeal-window/close")
    def close_appeal_window(case_id: str) -> dict[str, Any]:
        return appeals.close_expired_window(case_id)

    @app.post("/cases/{case_id}/runs/{case_run_id}/phases/{phase}")
    def submit_phase(case_id: str, case_run_id: str, phase: str, request: PhaseResultRequest) -> dict[str, Any]:
        return orchestrator.submit_phase_result(
            case_id,
            case_run_id=case_run_id,
            phase=phase,
            payload=request.payload,
            actor=request.actor,
            tokens_used=request.tokens_used,
        )

    @app.post("/cases/{case_id}/resume-evidence")
    def resume_evidence(case_id: str, request: ResumeEvidenceRequest) -> dict[str, Any]:
        return orchestrator.resume_with_evidence(
            case_id,
            target=request.target,
            evidence_id=request.evidence_id,
            question_ids=request.question_ids,
            actor_id=request.actor_id,
        )

    @app.post("/cases/{case_id}/evidence-and-resume")
    def submit_evidence_and_resume(case_id: str, request: EvidenceAndResumeRequest) -> dict[str, Any]:
        result = evidence_submission.submit_and_resume(
            case_id,
            target=request.target,
            question_ids=request.question_ids,
            evidence_type=request.evidence_type,
            description=request.description,
            source_record_id=request.source_record_id,
            captured_at=request.captured_at,
            extracted_facts=request.extracted_facts,
            related_claim_ids=request.related_claim_ids,
            content_sha256=request.content_sha256,
            immutable_uri=request.immutable_uri,
            source_system=request.source_system,
            handling_flags=request.handling_flags,
            actor_id=request.actor_id,
        )
        if request.auto_continue:
            job, _created = workflow_jobs.create_or_get_active(case_id, actor_id=request.actor_id)
            result["workflow_job"] = dispatch_workflow_job(job)
        return result

    @app.post("/cases/{case_id}/recover")
    def recover(case_id: str) -> dict[str, Any]:
        return orchestrator.recover(case_id)

    @app.get("/cases/{case_id}/replay")
    def replay(case_id: str) -> dict[str, Any]:
        return orchestrator.replay(case_id)

    @app.post("/tools/{tool_name:path}")
    def call_tool(tool_name: str, request: ToolCallRequest) -> Any:
        return tool_service.call(
            tool_name,
            request.parameters,
            actor=request.actor,
            case_run_id=request.case_run_id,
        )

    return app


app = create_app()
