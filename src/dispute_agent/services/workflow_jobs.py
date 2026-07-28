from __future__ import annotations

from datetime import timedelta
from threading import Event, Thread
from typing import Any

from rq.timeouts import JobTimeoutException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.agents.workflow import InvestigationWorkflow
from dispute_agent.agents.workflow_hooks import WorkflowHooks
from dispute_agent.config import Settings, get_settings
from dispute_agent.db import SessionLocal
from dispute_agent.errors import ConflictError, NotFoundError
from dispute_agent.ids import new_id
from dispute_agent.models import (
    Dispute,
    WorkflowJob,
    WorkflowJobEvent,
    WorkflowStage,
    utc_now,
)
from dispute_agent.serialization import jsonable, summarize


TERMINAL_JOB_STATUSES = {"CANCELLED", "COMPLETED", "FAILED", "TIMED_OUT"}
STARTABLE_CASE_STATES = {"SUBMITTED", "EVIDENCE_LOCKED", "UNDER_INVESTIGATION", "READY_FOR_REVIEW"}


class WorkflowPauseRequested(Exception):
    pass


class WorkflowCancelRequested(Exception):
    pass


class WorkflowAttemptTimedOut(Exception):
    pass


class WorkflowJobService:
    """Database-backed workflow job lifecycle; Redis is delivery, not state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        settings: Settings | None = None,
    ):
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    def create_or_get_active(self, case_id: str, *, actor_id: str) -> tuple[dict[str, Any], bool]:
        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            active = session.scalar(
                select(WorkflowJob).where(WorkflowJob.active_dedupe_key == case_id)
            )
            if active is not None:
                return self._view(session, active, deduplicated=True), False
            if dispute.state not in STARTABLE_CASE_STATES:
                raise ConflictError(f"案件当前状态不能启动 Agent 工作流: {dispute.state}")

            job_id = new_id("wjob")
            job = WorkflowJob(
                id=job_id,
                dispute_id=case_id,
                status="QUEUED",
                actor_id=actor_id,
                active_dedupe_key=case_id,
                rq_job_id=self.delivery_id(job_id, 1),
                delivery_version=1,
                attempt_count=0,
                max_attempts=self.settings.workflow_max_attempts,
                timeout_seconds=self.settings.workflow_job_timeout_seconds,
                event_sequence=0,
            )
            session.add(job)
            self._append_event(
                session,
                job,
                "JOB_QUEUED",
                payload={"actor_id": actor_id, "delivery_version": 1},
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                winner = session.scalar(
                    select(WorkflowJob).where(WorkflowJob.active_dedupe_key == case_id)
                )
                if winner is None:
                    raise
                return self._view(session, winner, deduplicated=True), False
            return self._view(session, job, deduplicated=False), True

    def get(self, job_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            return self._view(session, self._require_job(session, job_id))

    def latest_for_case(self, case_id: str) -> dict[str, Any] | None:
        with self.session_factory() as session:
            job = session.scalar(
                select(WorkflowJob)
                .where(WorkflowJob.dispute_id == case_id)
                .order_by(WorkflowJob.created_at.desc())
                .limit(1)
            )
            return self._view(session, job) if job else None

    def events_after(self, job_id: str, sequence: int, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            self._require_job(session, job_id)
            events = session.scalars(
                select(WorkflowJobEvent)
                .where(
                    WorkflowJobEvent.workflow_job_id == job_id,
                    WorkflowJobEvent.sequence > max(0, sequence),
                )
                .order_by(WorkflowJobEvent.sequence)
                .limit(min(max(limit, 1), 500))
            )
            return [self._event_view(item) for item in events]

    def begin_attempt(self, job_id: str, *, worker_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status in TERMINAL_JOB_STATUSES:
                return self._view(session, job)
            if job.status in {"PAUSED", "PAUSE_REQUESTED"}:
                raise WorkflowPauseRequested(job.paused_reason or "人工请求暂停")
            if job.status == "CANCEL_REQUESTED":
                raise WorkflowCancelRequested("人工请求取消")
            if job.attempt_count >= job.max_attempts:
                self._finish_failed(session, job, "ATTEMPTS_EXHAUSTED", "任务重试次数已耗尽")
                session.commit()
                return self._view(session, job)

            now = utc_now()
            for stage in session.scalars(
                select(WorkflowStage).where(
                    WorkflowStage.workflow_job_id == job.id,
                    WorkflowStage.status == "RUNNING",
                )
            ):
                stage.status = "PENDING"
                stage.error_message = "上一次 Worker 中断，等待幂等重放"
                stage.heartbeat_at = now

            job.attempt_count += 1
            job.status = "RUNNING"
            job.worker_id = worker_id
            job.heartbeat_at = now
            job.attempt_deadline_at = now + timedelta(seconds=job.timeout_seconds)
            job.started_at = job.started_at or now
            job.finished_at = None
            event_type = "JOB_STARTED" if job.attempt_count == 1 else "JOB_RETRY_STARTED"
            self._append_event(
                session,
                job,
                event_type,
                payload={"attempt": job.attempt_count, "max_attempts": job.max_attempts, "worker_id": worker_id},
            )
            session.commit()
            return self._view(session, job)

    def attach_case_run(self, job_id: str, case_run_id: str, phase: str) -> None:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            job.case_run_id = case_run_id
            job.current_phase = phase
            self._append_event(
                session,
                job,
                "CASE_RUN_ATTACHED",
                phase=phase,
                payload={"case_run_id": case_run_id},
            )
            session.commit()

    def check_control(self, job_id: str) -> None:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status in {"PAUSED", "PAUSE_REQUESTED"}:
                raise WorkflowPauseRequested(job.paused_reason or "人工请求暂停")
            if job.status in {"CANCELLED", "CANCEL_REQUESTED"}:
                raise WorkflowCancelRequested("人工请求取消")
            if job.attempt_deadline_at and utc_now() >= job.attempt_deadline_at:
                raise WorkflowAttemptTimedOut("工作流执行超过任务超时")

    def stage_started(self, job_id: str, phase: str, role: str, case_run_id: str) -> None:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            self._raise_if_controlled(job)
            stage = self._stage(session, job, phase, role, case_run_id)
            now = utc_now()
            stage.status = "RUNNING"
            stage.attempt_count += 1
            stage.started_at = stage.started_at or now
            stage.completed_at = None
            stage.heartbeat_at = now
            stage.error_message = None
            job.current_phase = phase
            job.case_run_id = case_run_id
            self._append_event(
                session,
                job,
                "STAGE_STARTED",
                phase=phase,
                agent_role=role,
                payload={"stage_attempt": stage.attempt_count},
            )
            session.commit()

    def stage_completed(
        self,
        job_id: str,
        phase: str,
        role: str,
        case_run_id: str,
        summary: dict[str, Any] | None,
    ) -> None:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            stage = self._stage(session, job, phase, role, case_run_id)
            now = utc_now()
            stage.status = "COMPLETED"
            stage.completed_at = now
            stage.heartbeat_at = now
            stage.result_summary_json = summarize(summary or {})
            self._append_event(
                session,
                job,
                "STAGE_COMPLETED",
                phase=phase,
                agent_role=role,
                payload=stage.result_summary_json,
            )
            session.commit()

    def stage_failed(
        self,
        job_id: str,
        phase: str,
        role: str,
        case_run_id: str,
        error: Exception,
    ) -> None:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            stage = self._stage(session, job, phase, role, case_run_id)
            stage.status = "FAILED"
            stage.completed_at = utc_now()
            stage.error_message = self._safe_error(error)
            self._append_event(
                session,
                job,
                "STAGE_FAILED",
                phase=phase,
                agent_role=role,
                payload={"error": stage.error_message},
            )
            session.commit()

    def phase_committed(self, job_id: str, phase: str, view: dict[str, Any]) -> None:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            job.current_phase = view.get("phase")
            job.case_run_id = view.get("case_run_id") or job.case_run_id
            self._append_event(
                session,
                job,
                "PHASE_COMMITTED",
                phase=phase,
                payload={"next_phase": view.get("phase"), "case_state": view.get("state")},
            )
            session.commit()

    def boundary_reached(self, job_id: str, boundary: str, view: dict[str, Any]) -> None:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            job.current_phase = view.get("phase")
            self._append_event(
                session,
                job,
                "WORKFLOW_BOUNDARY_REACHED",
                phase=view.get("phase"),
                payload={"boundary": boundary, "case_state": view.get("state")},
            )
            session.commit()

    def heartbeat(self, job_id: str) -> None:
        with self.session_factory() as session:
            job = session.get(WorkflowJob, job_id)
            if job is None or job.status not in {"RUNNING", "PAUSE_REQUESTED", "CANCEL_REQUESTED"}:
                return
            now = utc_now()
            job.heartbeat_at = now
            for stage in session.scalars(
                select(WorkflowStage).where(
                    WorkflowStage.workflow_job_id == job_id,
                    WorkflowStage.status == "RUNNING",
                )
            ):
                stage.heartbeat_at = now
            session.commit()

    def complete(self, job_id: str, result: dict[str, Any]) -> dict[str, Any]:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            now = utc_now()
            job.status = "COMPLETED"
            job.result_json = jsonable(result)
            job.finished_at = now
            job.heartbeat_at = now
            job.active_dedupe_key = None
            job.error_code = None
            job.error_message = None
            self._append_event(
                session,
                job,
                "JOB_COMPLETED",
                phase=job.current_phase,
                payload={"workflow_boundary": result.get("workflow_boundary"), "case_state": result.get("state")},
            )
            session.commit()
            return self._view(session, job)

    def fail_attempt(self, job_id: str, error: Exception, *, timed_out: bool = False) -> dict[str, Any]:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            message = self._safe_error(error)
            code = "WORKFLOW_TIMEOUT" if timed_out else getattr(error, "code", type(error).__name__)
            job.error_code = str(code)[:100]
            job.error_message = message
            job.heartbeat_at = utc_now()
            if job.attempt_count < job.max_attempts:
                job.status = "RETRYING"
                event_type = "JOB_RETRY_SCHEDULED"
            else:
                job.status = "TIMED_OUT" if timed_out else "FAILED"
                job.finished_at = utc_now()
                job.active_dedupe_key = None
                event_type = "JOB_TIMED_OUT" if timed_out else "JOB_FAILED"
            self._append_event(
                session,
                job,
                event_type,
                phase=job.current_phase,
                payload={
                    "attempt": job.attempt_count,
                    "max_attempts": job.max_attempts,
                    "error_code": job.error_code,
                    "error": message,
                },
            )
            session.commit()
            return self._view(session, job)

    def request_pause(self, job_id: str, *, actor_id: str, reason: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status in TERMINAL_JOB_STATUSES:
                raise ConflictError(f"终态任务不能暂停: {job.status}")
            if job.status == "PAUSED":
                return self._view(session, job)
            now = utc_now()
            job.pause_requested_at = now
            job.paused_reason = reason
            if job.status in {"QUEUED", "RETRYING"}:
                job.status = "PAUSED"
                event_type = "JOB_PAUSED"
            else:
                job.status = "PAUSE_REQUESTED"
                event_type = "JOB_PAUSE_REQUESTED"
            self._append_event(
                session,
                job,
                event_type,
                phase=job.current_phase,
                payload={"actor_id": actor_id, "reason": reason},
            )
            session.commit()
            return self._view(session, job)

    def confirm_paused(self, job_id: str, reason: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status != "PAUSED":
                job.status = "PAUSED"
                job.paused_reason = reason
                job.heartbeat_at = utc_now()
                for stage in session.scalars(
                    select(WorkflowStage).where(
                        WorkflowStage.workflow_job_id == job.id,
                        WorkflowStage.status == "RUNNING",
                    )
                ):
                    stage.status = "PAUSED"
                self._append_event(
                    session,
                    job,
                    "JOB_PAUSED",
                    phase=job.current_phase,
                    payload={"reason": reason},
                )
                session.commit()
            return self._view(session, job)

    def request_cancel(self, job_id: str, *, actor_id: str, reason: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status == "CANCELLED":
                return self._view(session, job)
            if job.status in {"COMPLETED", "FAILED", "TIMED_OUT"}:
                raise ConflictError(f"终态任务不能取消: {job.status}")
            job.cancel_requested_at = utc_now()
            if job.status in {"QUEUED", "RETRYING", "PAUSED"}:
                self._cancel_in_session(session, job, reason)
                event_type = "JOB_CANCELLED"
            else:
                job.status = "CANCEL_REQUESTED"
                event_type = "JOB_CANCEL_REQUESTED"
            self._append_event(
                session,
                job,
                event_type,
                phase=job.current_phase,
                payload={"actor_id": actor_id, "reason": reason},
            )
            session.commit()
            return self._view(session, job)

    def confirm_cancelled(self, job_id: str, reason: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status != "CANCELLED":
                self._cancel_in_session(session, job, reason)
                self._append_event(
                    session,
                    job,
                    "JOB_CANCELLED",
                    phase=job.current_phase,
                    payload={"reason": reason},
                )
                session.commit()
            return self._view(session, job)

    def resume(self, job_id: str, *, actor_id: str, reason: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status != "PAUSED":
                raise ConflictError(f"只有 PAUSED 任务可以恢复: {job.status}")
            job.delivery_version += 1
            job.rq_job_id = self.delivery_id(job.id, job.delivery_version)
            job.status = "QUEUED"
            job.pause_requested_at = None
            job.paused_reason = None
            job.worker_id = None
            for stage in session.scalars(
                select(WorkflowStage).where(
                    WorkflowStage.workflow_job_id == job.id,
                    WorkflowStage.status == "PAUSED",
                )
            ):
                stage.status = "PENDING"
            self._append_event(
                session,
                job,
                "JOB_RESUMED",
                phase=job.current_phase,
                payload={
                    "actor_id": actor_id,
                    "reason": reason,
                    "delivery_version": job.delivery_version,
                },
            )
            session.commit()
            return self._view(session, job)

    def record_dispatch_error(self, job_id: str, error: Exception) -> None:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            job.error_code = "QUEUE_UNAVAILABLE"
            job.error_message = self._safe_error(error)
            self._append_event(
                session,
                job,
                "JOB_DISPATCH_FAILED",
                payload={"error": job.error_message, "rq_job_id": job.rq_job_id},
            )
            session.commit()

    def record_dispatched(self, job_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status in {"QUEUED", "RETRYING"}:
                job.error_code = None
                job.error_message = None
                self._append_event(
                    session,
                    job,
                    "JOB_DISPATCHED",
                    payload={"rq_job_id": job.rq_job_id, "delivery_version": job.delivery_version},
                )
                session.commit()
            return self._view(session, job)

    def pending_dispatches(self) -> list[dict[str, Any]]:
        """Database outbox rows safe to idempotently enqueue on Worker startup."""
        with self.session_factory() as session:
            jobs = session.scalars(
                select(WorkflowJob)
                .where(WorkflowJob.status.in_({"QUEUED", "RETRYING"}))
                .order_by(WorkflowJob.created_at)
            )
            return [self._view(session, job) for job in jobs]

    def recover_stale(self) -> list[dict[str, Any]]:
        cutoff = utc_now() - timedelta(seconds=self.settings.workflow_stale_after_seconds)
        recovered: list[dict[str, Any]] = []
        with self.session_factory() as session:
            jobs = list(
                session.scalars(
                    select(WorkflowJob).where(
                        WorkflowJob.status.in_({"RUNNING", "PAUSE_REQUESTED", "CANCEL_REQUESTED"}),
                        WorkflowJob.heartbeat_at.is_not(None),
                        WorkflowJob.heartbeat_at < cutoff,
                    )
                )
            )
            for job in jobs:
                if job.status == "PAUSE_REQUESTED":
                    job.status = "PAUSED"
                    event_type = "STALE_JOB_PAUSED"
                elif job.status == "CANCEL_REQUESTED":
                    self._cancel_in_session(session, job, "Worker 中断后完成取消")
                    event_type = "STALE_JOB_CANCELLED"
                elif job.attempt_count >= job.max_attempts:
                    self._finish_failed(session, job, "WORKER_LOST", "Worker 心跳过期且重试次数已耗尽")
                    event_type = "STALE_JOB_FAILED"
                else:
                    job.status = "RETRYING"
                    job.delivery_version += 1
                    job.rq_job_id = self.delivery_id(job.id, job.delivery_version)
                    job.worker_id = None
                    event_type = "STALE_JOB_RECOVERED"
                self._append_event(
                    session,
                    job,
                    event_type,
                    phase=job.current_phase,
                    payload={"stale_after_seconds": self.settings.workflow_stale_after_seconds},
                )
                recovered.append(self._view(session, job))
            session.commit()
        return recovered

    @staticmethod
    def delivery_id(job_id: str, version: int) -> str:
        return f"xianyu-{job_id}-v{version}"

    def _stage(
        self,
        session: Session,
        job: WorkflowJob,
        phase: str,
        role: str,
        case_run_id: str,
    ) -> WorkflowStage:
        stage = session.scalar(
            select(WorkflowStage).where(
                WorkflowStage.workflow_job_id == job.id,
                WorkflowStage.phase == phase,
                WorkflowStage.agent_role == role,
            )
        )
        if stage is None:
            stage = WorkflowStage(
                id=new_id("wstage"),
                workflow_job_id=job.id,
                dispute_id=job.dispute_id,
                case_run_id=case_run_id,
                phase=phase,
                agent_role=role,
                status="PENDING",
                attempt_count=0,
            )
            session.add(stage)
        else:
            stage.case_run_id = case_run_id
        return stage

    def _append_event(
        self,
        session: Session,
        job: WorkflowJob,
        event_type: str,
        *,
        phase: str | None = None,
        agent_role: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if job in session.new:
            job.event_sequence = (job.event_sequence or 0) + 1
        else:
            sequence = session.scalar(
                update(WorkflowJob)
                .where(WorkflowJob.id == job.id)
                .values(event_sequence=WorkflowJob.event_sequence + 1)
                .returning(WorkflowJob.event_sequence)
            )
            if sequence is None:
                raise NotFoundError(f"Workflow Job 不存在: {job.id}")
            job.event_sequence = sequence
        session.add(
            WorkflowJobEvent(
                id=new_id("wjevt"),
                workflow_job_id=job.id,
                sequence=job.event_sequence,
                event_type=event_type,
                phase=phase,
                agent_role=agent_role,
                payload_json=jsonable(payload or {}),
            )
        )

    @staticmethod
    def _raise_if_controlled(job: WorkflowJob) -> None:
        if job.status in {"PAUSED", "PAUSE_REQUESTED"}:
            raise WorkflowPauseRequested(job.paused_reason or "人工请求暂停")
        if job.status in {"CANCELLED", "CANCEL_REQUESTED"}:
            raise WorkflowCancelRequested("人工请求取消")

    @staticmethod
    def _cancel_in_session(session: Session, job: WorkflowJob, reason: str) -> None:
        now = utc_now()
        job.status = "CANCELLED"
        job.active_dedupe_key = None
        job.finished_at = now
        job.heartbeat_at = now
        job.error_code = "CANCELLED_BY_USER"
        job.error_message = reason
        for stage in session.scalars(
            select(WorkflowStage).where(
                WorkflowStage.workflow_job_id == job.id,
                WorkflowStage.status.in_({"PENDING", "RUNNING", "PAUSED"}),
            )
        ):
            stage.status = "CANCELLED"
            stage.completed_at = now

    @staticmethod
    def _finish_failed(session: Session, job: WorkflowJob, code: str, message: str) -> None:
        job.status = "FAILED"
        job.active_dedupe_key = None
        job.finished_at = utc_now()
        job.error_code = code
        job.error_message = message

    @staticmethod
    def _require_job(session: Session, job_id: str) -> WorkflowJob:
        job = session.get(WorkflowJob, job_id)
        if job is None:
            raise NotFoundError(f"Workflow Job 不存在: {job_id}")
        return job

    def _view(
        self,
        session: Session,
        job: WorkflowJob,
        *,
        deduplicated: bool = False,
    ) -> dict[str, Any]:
        stages = list(
            session.scalars(
                select(WorkflowStage)
                .where(WorkflowStage.workflow_job_id == job.id)
                .order_by(WorkflowStage.created_at, WorkflowStage.agent_role)
            )
        )
        return jsonable(
            {
                "job_id": job.id,
                "case_id": job.dispute_id,
                "case_run_id": job.case_run_id,
                "status": job.status,
                "current_phase": job.current_phase,
                "actor_id": job.actor_id,
                "rq_job_id": job.rq_job_id,
                "delivery_version": job.delivery_version,
                "attempt_count": job.attempt_count,
                "max_attempts": job.max_attempts,
                "timeout_seconds": job.timeout_seconds,
                "worker_id": job.worker_id,
                "heartbeat_at": job.heartbeat_at,
                "attempt_deadline_at": job.attempt_deadline_at,
                "pause_requested_at": job.pause_requested_at,
                "cancel_requested_at": job.cancel_requested_at,
                "paused_reason": job.paused_reason,
                "error": (
                    {"code": job.error_code, "message": job.error_message}
                    if job.error_code or job.error_message
                    else None
                ),
                "result": job.result_json,
                "event_sequence": job.event_sequence,
                "deduplicated": deduplicated,
                "terminal": job.status in TERMINAL_JOB_STATUSES,
                "created_at": job.created_at,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "updated_at": job.updated_at,
                "events_url": f"/workflow-jobs/{job.id}/events",
                "stages": [self._stage_view(item) for item in stages],
            }
        )

    @staticmethod
    def _stage_view(stage: WorkflowStage) -> dict[str, Any]:
        return jsonable(
            {
                "stage_id": stage.id,
                "phase": stage.phase,
                "agent_role": stage.agent_role,
                "status": stage.status,
                "attempt_count": stage.attempt_count,
                "heartbeat_at": stage.heartbeat_at,
                "result_summary": stage.result_summary_json,
                "error_message": stage.error_message,
                "started_at": stage.started_at,
                "completed_at": stage.completed_at,
            }
        )

    @staticmethod
    def _event_view(event: WorkflowJobEvent) -> dict[str, Any]:
        return jsonable(
            {
                "sequence": event.sequence,
                "event_type": event.event_type,
                "phase": event.phase,
                "agent_role": event.agent_role,
                "payload": event.payload_json,
                "occurred_at": event.occurred_at,
            }
        )

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error).strip() or type(error).__name__
        return message[:2000]


class DatabaseWorkflowHooks(WorkflowHooks):
    def __init__(self, jobs: WorkflowJobService, job_id: str):
        self.jobs = jobs
        self.job_id = job_id

    def run_started(self, case_run_id: str, phase: str) -> None:
        self.jobs.attach_case_run(self.job_id, case_run_id, phase)

    def check_control(self) -> None:
        self.jobs.check_control(self.job_id)

    def stage_started(self, phase: str, agent_role: str, case_run_id: str) -> None:
        self.jobs.stage_started(self.job_id, phase, agent_role, case_run_id)

    def stage_completed(
        self,
        phase: str,
        agent_role: str,
        case_run_id: str,
        summary: dict[str, Any] | None = None,
    ) -> None:
        self.jobs.stage_completed(self.job_id, phase, agent_role, case_run_id, summary)

    def stage_failed(self, phase: str, agent_role: str, case_run_id: str, error: Exception) -> None:
        self.jobs.stage_failed(self.job_id, phase, agent_role, case_run_id, error)

    def phase_committed(self, phase: str, view: dict[str, Any]) -> None:
        self.jobs.phase_committed(self.job_id, phase, view)

    def boundary_reached(self, boundary: str, view: dict[str, Any]) -> None:
        self.jobs.boundary_reached(self.job_id, boundary, view)


class WorkflowHeartbeat:
    def __init__(self, jobs: WorkflowJobService, job_id: str, interval_seconds: float):
        self.jobs = jobs
        self.job_id = job_id
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._thread = Thread(target=self._run, name=f"heartbeat-{job_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, self.interval_seconds + 1.0))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.jobs.heartbeat(self.job_id)
            except Exception:
                # The execution path remains authoritative. A transient heartbeat
                # write failure is retried on the next interval.
                continue


class WorkflowJobExecutor:
    def __init__(
        self,
        jobs: WorkflowJobService,
        workflow: InvestigationWorkflow,
        *,
        settings: Settings | None = None,
    ):
        self.jobs = jobs
        self.workflow = workflow
        self.settings = settings or get_settings()

    def execute(self, job_id: str, *, worker_id: str) -> dict[str, Any]:
        try:
            started = self.jobs.begin_attempt(job_id, worker_id=worker_id)
        except WorkflowPauseRequested as exc:
            return self.jobs.confirm_paused(job_id, str(exc))
        except WorkflowCancelRequested as exc:
            return self.jobs.confirm_cancelled(job_id, str(exc))
        if started["terminal"]:
            return started

        heartbeat = WorkflowHeartbeat(
            self.jobs,
            job_id,
            self.settings.workflow_heartbeat_interval_seconds,
        )
        heartbeat.start()
        try:
            result = self.workflow.run_until_blocked(
                started["case_id"],
                actor_id=started["actor_id"],
                hooks=DatabaseWorkflowHooks(self.jobs, job_id),
            )
        except WorkflowPauseRequested as exc:
            return self.jobs.confirm_paused(job_id, str(exc))
        except WorkflowCancelRequested as exc:
            return self.jobs.confirm_cancelled(job_id, str(exc))
        except (WorkflowAttemptTimedOut, JobTimeoutException) as exc:
            self.jobs.fail_attempt(job_id, exc, timed_out=True)
            raise
        except Exception as exc:
            self.jobs.fail_attempt(job_id, exc)
            raise
        else:
            return self.jobs.complete(job_id, result)
        finally:
            heartbeat.stop()
