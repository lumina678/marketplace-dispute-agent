from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.db import SessionLocal
from dispute_agent.errors import DisputeAgentError, NotFoundError
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
from dispute_agent.config import PROJECT_ROOT, Settings, get_settings
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
) -> FastAPI:
    settings = settings or get_settings()
    backend = generation_backend or create_generation_backend(settings)
    app = FastAPI(
        title="Marketplace Dispute Agent",
        version="0.1.0",
        description="Multi-agent investigation and adjudication support for second-hand marketplace disputes.",
    )
    tool_service = ToolService(session_factory)
    orchestrator = CaseOrchestrator(session_factory, settings=settings)
    runtime = AgentRuntime(session_factory, backend=backend, tools=tool_service)
    decision_guard = DecisionGuard(session_factory)
    workflow = InvestigationWorkflow(
        session_factory,
        orchestrator=orchestrator,
        runtime=runtime,
        tools=tool_service,
        decision_guard=decision_guard,
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
    skill_registry = get_skill_registry()
    app.state.generation_backend = backend
    app.state.skill_registry = skill_registry

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
            else 409
            if exc.code in {"CONFLICT", "INVALID_STATE_TRANSITION"}
            else 502
            if exc.code == "MODEL_BACKEND_ERROR"
            else 400
        )
        return JSONResponse(status_code=status, content={"error": {"code": exc.code, "message": str(exc)}})

    @app.get("/health")
    def health(session: Session = Depends(get_session)) -> dict[str, str]:
        session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/model")
    def model_configuration() -> dict[str, Any]:
        return backend.describe()

    @app.get("/model/health")
    def model_health(probe: bool = Query(default=True)) -> dict[str, Any]:
        return backend.health_check(probe=probe)

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
        return workbench.get(case_id)

    @app.post("/cases/{case_id}/demo-reset")
    def reset_text_demo(case_id: str) -> dict[str, Any]:
        return demo_cases.reset_text_demo(case_id)

    @app.get("/evaluation")
    def evaluate_cases(case_id: list[str] | None = Query(default=None)) -> dict[str, Any]:
        return evaluation.evaluate(case_ids=case_id)

    @app.get("/workbench", response_class=HTMLResponse)
    def workbench_page() -> str:
        page = PROJECT_ROOT / "web" / "workbench.html"
        return page.read_text(encoding="utf-8")

    @app.post("/cases/{case_id}/runs")
    def start_run(case_id: str) -> dict[str, Any]:
        return orchestrator.start(case_id)

    @app.post("/cases/{case_id}/workflow")
    def run_workflow(case_id: str, request: WorkflowRequest | None = None) -> dict[str, Any]:
        return workflow.run_until_blocked(
            case_id,
            actor_id=request.actor_id if request else "investigation-workflow",
        )

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
            result["workflow"] = workflow.run_until_blocked(case_id, actor_id=request.actor_id)
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
