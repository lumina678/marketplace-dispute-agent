from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.config import PROJECT_ROOT
from dispute_agent.db import SessionLocal
from dispute_agent.errors import NotFoundError, ValidationError
from dispute_agent.models import (
    Appeal,
    AgentOutput,
    Approval,
    CaseRun,
    Claim,
    Decision,
    Dispute,
    Evidence,
    OpenQuestion,
    ResolutionAction,
    ToolCall,
)
from dispute_agent.serialization import jsonable


DEFAULT_LABELS_PATH = PROJECT_ROOT / "evaluation" / "labels.json"


class EvaluationService:
    """Score persisted case artifacts without changing the case database."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        labels_path: Path = DEFAULT_LABELS_PATH,
    ):
        self.session_factory = session_factory
        self.labels_path = Path(labels_path)

    def evaluate(self, *, case_ids: list[str] | None = None) -> dict[str, Any]:
        labels = self._load_labels()
        selected = set(case_ids or [])
        if selected:
            unknown = selected - {item["case_id"] for item in labels["cases"]}
            if unknown:
                raise ValidationError(f"评测标签不存在: {', '.join(sorted(unknown))}")
            label_items = [item for item in labels["cases"] if item["case_id"] in selected]
        else:
            label_items = labels["cases"]

        with self.session_factory() as session:
            details = [self._score_case(session, item) for item in label_items]
            metrics = self._aggregate(details)
            scope_ids = [item["case_id"] for item in label_items]
            tool_calls = list(
                session.scalars(select(ToolCall).where(ToolCall.dispute_id.in_(scope_ids)))
            ) if scope_ids else []
            runs = list(
                session.scalars(select(CaseRun).where(CaseRun.dispute_id.in_(scope_ids)))
            ) if scope_ids else []
            metrics["cost"] = {
                "tool_calls": len(tool_calls),
                "failed_tool_calls": sum(item.status == "FAILED" for item in tool_calls),
                "duration_ms": sum(item.duration_ms for item in tool_calls),
                "tokens_used": sum(item.tokens_used for item in runs),
                "case_runs": len(runs),
            }
            return jsonable(
                {
                    "schema_version": "1.0.0",
                    "suite_id": labels["suite_id"],
                    "labels_path": str(self.labels_path),
                    "cases_total": len(details),
                    "metrics": metrics,
                    "cases": details,
                }
            )

    def _load_labels(self) -> dict[str, Any]:
        if not self.labels_path.exists():
            raise NotFoundError(f"评测标签文件不存在: {self.labels_path}")
        try:
            payload = json.loads(self.labels_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"评测标签不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
            raise ValidationError("评测标签必须包含 cases 数组")
        for item in payload["cases"]:
            if not isinstance(item, dict) or not item.get("case_id"):
                raise ValidationError("每个评测标签必须包含 case_id")
        return payload

    def _score_case(self, session: Session, label: dict[str, Any]) -> dict[str, Any]:
        case_id = label["case_id"]
        dispute = session.get(Dispute, case_id)
        if dispute is None:
            return {"case_id": case_id, "available": False, "reason": "CASE_NOT_FOUND"}

        decision = session.scalar(
            select(Decision).where(Decision.dispute_id == case_id).order_by(Decision.version.desc()).limit(1)
        )
        claims = list(session.scalars(select(Claim).where(Claim.dispute_id == case_id)))
        evidence = list(session.scalars(select(Evidence).where(Evidence.dispute_id == case_id)))
        questions = list(session.scalars(select(OpenQuestion).where(OpenQuestion.dispute_id == case_id)))
        actions = list(session.scalars(select(ResolutionAction).where(ResolutionAction.dispute_id == case_id)))
        approvals = list(session.scalars(select(Approval).where(Approval.dispute_id == case_id)))
        appeals = list(
            session.scalars(select(Appeal).where(Appeal.dispute_id == case_id).order_by(Appeal.submitted_at))
        )
        outputs = list(session.scalars(select(AgentOutput).where(AgentOutput.dispute_id == case_id)))

        expected_questions = label.get("expected_question_required")
        actual_questions = bool(questions)
        decision_payload = decision.payload_json if decision is not None else {}
        actual_outcome = decision.outcome if decision is not None else None
        expected_outcome = label.get("expected_outcome") if label.get("score_decision", True) else None
        outcome_match = actual_outcome == expected_outcome if expected_outcome and decision is not None else None

        policy_version = decision_payload.get("policy", {}).get("version")
        clerk_citations = {
            citation.get("citation_id"): citation
            for item in outputs
            if item.role == "EVIDENCE_POLICY_CLERK" and item.output_type == "EVIDENCE_POLICY_REPORT"
            for citation in item.payload_json.get("policy_citations", [])
        }
        if policy_version is None:
            reports = [
                item.payload_json
                for item in outputs
                if item.role == "EVIDENCE_POLICY_CLERK" and item.output_type == "EVIDENCE_POLICY_REPORT"
            ]
            policy_version = reports[-1].get("policy_version") if reports else None
        decision_citation_ids = {
            citation_id
            for finding in decision_payload.get("claim_findings", [])
            for citation_id in finding.get("policy_citation_ids", [])
        }
        citations_valid = bool(decision_citation_ids) and all(
            clerk_citations.get(citation_id, {}).get("policy_version") == label.get("expected_policy_version")
            for citation_id in decision_citation_ids
        ) if decision is not None else False
        policy_match = (
            policy_version == label.get("expected_policy_version") and citations_valid
            if label.get("expected_policy_version") and decision is not None
            else None
        )

        expected_evidence = set(label.get("key_evidence_ids", []))
        cited_evidence: set[str] = set()
        unsupported_facts = 0
        total_facts = 0
        if decision is not None:
            for finding in decision_payload.get("claim_findings", []):
                cited_evidence.update(finding.get("evidence_ids", []))
            for fact in decision_payload.get("established_facts", []):
                total_facts += 1
                refs = set(fact.get("evidence_ids", []))
                cited_evidence.update(refs)
                if not refs:
                    unsupported_facts += 1
        key_recall = (
            len(expected_evidence & cited_evidence) / len(expected_evidence)
            if expected_evidence and decision is not None
            else None
        )

        actual_winner = self._winner(actual_outcome)
        expected_winner = label.get("expected_winner")
        winner_match = (
            actual_winner == expected_winner
            if expected_winner in {"BUYER", "SELLER", "NONE"} and decision is not None
            else None
        )
        successful_refunds = sum(
            item.status == "SUCCEEDED" and item.action_type in {"FULL_REFUND", "PARTIAL_REFUND"}
            for item in actions
        )
        valid_approval_decision_ids = {
            item.decision_id
            for item in approvals
            if item.action == "APPROVE"
            and (approved := session.get(Decision, item.decision_id)) is not None
            and approved.status == "APPROVED"
            and item.decision_content_sha256 == approved.content_sha256
        }
        successful_action_count = sum(item.status == "SUCCEEDED" for item in actions)
        unauthorized_execution_count = sum(
            item.status == "SUCCEEDED" and item.decision_id not in valid_approval_decision_ids
            for item in actions
        )
        duplicate_refunds = max(0, successful_refunds - 1)
        accepted_appeals = sum(item.status == "ACCEPTED" for item in appeals)
        latest_version = decision.version if decision is not None else 0
        appeal_correction = bool(accepted_appeals and latest_version > 1)

        return {
            "case_id": case_id,
            "available": True,
            "state": dispute.state,
            "decision_id": decision.id if decision is not None else None,
            "decision_version": latest_version,
            "actual_outcome": actual_outcome,
            "expected_outcome": expected_outcome,
            "outcome_match": outcome_match,
            "policy_version": policy_version,
            "expected_policy_version": label.get("expected_policy_version"),
            "policy_match": policy_match,
            "key_evidence_recall": key_recall,
            "unsupported_fact_ratio": (unsupported_facts / total_facts if total_facts else 0.0) if decision else None,
            "question_required_expected": expected_questions,
            "question_required_actual": actual_questions,
            "question_match": actual_questions == expected_questions if expected_questions is not None else None,
            "human_review_expected": label.get("expected_human_review"),
            "human_review_actual": decision.requires_human_review if decision is not None else None,
            "human_review_match": (
                decision.requires_human_review == label.get("expected_human_review")
                if decision is not None and label.get("expected_human_review") is not None
                else None
            ),
            "winner_match": winner_match,
            "successful_refund_actions": successful_refunds,
            "duplicate_refund_count": duplicate_refunds,
            "successful_action_count": successful_action_count,
            "unauthorized_execution_count": unauthorized_execution_count,
            "appeal_count": len(appeals),
            "accepted_appeal_count": accepted_appeals,
            "appeal_correction": appeal_correction,
            "tool_calls": session.scalar(
                select(func.count()).select_from(ToolCall).where(ToolCall.dispute_id == case_id)
            )
            or 0,
            "agent_outputs": len(outputs),
            "claims": len(claims),
            "evidence": len(evidence),
            "questions": len(questions),
        }

    @staticmethod
    def _winner(outcome: str | None) -> str:
        if outcome in {"RETURN_AND_FULL_REFUND", "PARTIAL_REFUND"}:
            return "BUYER"
        if outcome in {"RELEASE_FUNDS"}:
            return "SELLER"
        return "NONE"

    @staticmethod
    def _mean(details: list[dict[str, Any]], field: str) -> dict[str, Any]:
        values = [item[field] for item in details if isinstance(item.get(field), (int, float))]
        return {
            "value": sum(values) / len(values) if values else None,
            "scored_cases": len(values),
        }

    def _aggregate(self, details: list[dict[str, Any]]) -> dict[str, Any]:
        scored = [item for item in details if item.get("available")]
        return {
            "outcome_accuracy": self._mean(scored, "outcome_match"),
            "policy_citation_accuracy": self._mean(scored, "policy_match"),
            "key_evidence_recall": self._mean(scored, "key_evidence_recall"),
            "unsupported_fact_ratio": self._mean(scored, "unsupported_fact_ratio"),
            "question_accuracy": self._mean(scored, "question_match"),
            "human_review_accuracy": self._mean(scored, "human_review_match"),
            "party_bias_rate": {
                "value": 1 - self._mean(scored, "winner_match")["value"]
                if self._mean(scored, "winner_match")["value"] is not None
                else None,
                "scored_cases": self._mean(scored, "winner_match")["scored_cases"],
            },
            "duplicate_refund_count": sum(item.get("duplicate_refund_count", 0) for item in scored),
            "unauthorized_execution_rate": {
                "value": sum(item.get("unauthorized_execution_count", 0) for item in scored)
                / sum(item.get("successful_action_count", 0) for item in scored)
                if any(item.get("successful_action_count", 0) for item in scored)
                else 0.0,
                "successful_actions": sum(item.get("successful_action_count", 0) for item in scored),
            },
            "appeal_correction_rate": {
                "value": sum(item.get("appeal_correction", False) for item in scored) / sum(item.get("accepted_appeal_count", 0) > 0 for item in scored)
                if any(item.get("accepted_appeal_count", 0) > 0 for item in scored)
                else None,
                "scored_cases": sum(item.get("accepted_appeal_count", 0) > 0 for item in scored),
            },
        }
