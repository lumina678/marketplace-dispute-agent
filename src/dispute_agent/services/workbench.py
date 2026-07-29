from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.db import SessionLocal
from dispute_agent.errors import NotFoundError
from dispute_agent.models import (
    AgentOutput,
    Approval,
    Appeal,
    CaseEvent,
    CaseRun,
    Claim,
    ClaimRoutingDecision,
    Decision,
    DecisionGuardReport,
    Dispute,
    Evidence,
    ListingSnapshot,
    Message,
    OpenQuestion,
    ResolutionAction,
    ShipmentEvent,
    Transaction,
)
from dispute_agent.serialization import jsonable
from dispute_agent.services.policy import PolicyService
from dispute_agent.services.claim_routing import case_routing_summary, claim_routing_payload


class WorkbenchService:
    """Build a read-only, reviewer-facing case workbench projection."""

    def __init__(self, session_factory: sessionmaker[Session] = SessionLocal):
        self.session_factory = session_factory

    def get(self, case_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            transaction = session.get(Transaction, dispute.transaction_id)
            assert transaction is not None
            claims = list(session.scalars(select(Claim).where(Claim.dispute_id == case_id).order_by(Claim.asserted_at)))
            evidence = list(
                session.scalars(select(Evidence).where(Evidence.dispute_id == case_id).order_by(Evidence.submitted_at))
            )
            listing = session.scalar(
                select(ListingSnapshot).where(
                    ListingSnapshot.transaction_id == transaction.id,
                    ListingSnapshot.snapshot_type == "DISPUTE_BASELINE",
                )
            )
            messages = list(session.scalars(select(Message).where(Message.transaction_id == transaction.id).order_by(Message.sent_at)))
            shipment = list(
                session.scalars(select(ShipmentEvent).where(ShipmentEvent.transaction_id == transaction.id).order_by(ShipmentEvent.occurred_at))
            )
            events = list(session.scalars(select(CaseEvent).where(CaseEvent.dispute_id == case_id).order_by(CaseEvent.sequence)))
            runs = list(session.scalars(select(CaseRun).where(CaseRun.dispute_id == case_id).order_by(CaseRun.run_number)))
            outputs = list(
                session.scalars(select(AgentOutput).where(AgentOutput.dispute_id == case_id).order_by(AgentOutput.created_at))
            )
            decisions = list(
                session.scalars(select(Decision).where(Decision.dispute_id == case_id).order_by(Decision.version))
            )
            approvals = list(
                session.scalars(select(Approval).where(Approval.dispute_id == case_id).order_by(Approval.created_at))
            )
            guard_reports = list(
                session.scalars(
                    select(DecisionGuardReport)
                    .where(DecisionGuardReport.dispute_id == case_id)
                    .order_by(DecisionGuardReport.created_at)
                )
            )
            actions = list(
                session.scalars(select(ResolutionAction).where(ResolutionAction.dispute_id == case_id).order_by(ResolutionAction.created_at))
            )
            appeals = list(
                session.scalars(select(Appeal).where(Appeal.dispute_id == case_id).order_by(Appeal.submitted_at))
            )
            questions = list(
                session.scalars(select(OpenQuestion).where(OpenQuestion.dispute_id == case_id).order_by(OpenQuestion.created_at))
            )
            routing_decisions = list(
                session.scalars(
                    select(ClaimRoutingDecision)
                    .where(ClaimRoutingDecision.dispute_id == case_id)
                    .order_by(ClaimRoutingDecision.claim_id, ClaimRoutingDecision.decision_version)
                )
            )
            policy = None
            if dispute.policy_id and dispute.policy_version:
                policy = PolicyService(session).get_version(dispute.policy_id, dispute.policy_version)

            latest = decisions[-1] if decisions else None
            citation_index = self._citation_index(outputs)
            return jsonable(
                {
                    "case": {
                        "case_id": dispute.id,
                        "transaction_id": transaction.id,
                        "dispute_type": dispute.dispute_type,
                        "state": dispute.state,
                        "state_version": dispute.state_version,
                        "requires_human_review": dispute.requires_human_review,
                        "human_review_reasons": dispute.human_review_reasons_json,
                        "active_case_run_id": dispute.active_case_run_id,
                        "created_at": dispute.created_at,
                        "updated_at": dispute.updated_at,
                    },
                    "parties": {
                        "buyer_id": transaction.buyer_id,
                        "buyer_name": transaction.buyer.display_name,
                        "seller_id": transaction.seller_id,
                        "seller_name": transaction.seller.display_name,
                    },
                    "transaction": {
                        "transaction_id": transaction.id,
                        "category": transaction.category,
                        "paid_amount_minor": transaction.paid_amount_minor,
                        "currency": transaction.currency,
                        "paid_at": transaction.paid_at,
                        "delivered_at": transaction.delivered_at,
                        "order_status": transaction.order_status,
                        "funds_status": transaction.funds_status,
                    },
                    "claims": [
                        {
                            "claim_id": item.id,
                            "party": item.party,
                            "claim_type": item.claim_type,
                            **claim_routing_payload(item),
                            "statement": item.statement,
                            "status": item.status,
                            "material": item.material,
                            "responds_to_claim_id": item.responds_to_claim_id,
                        }
                        for item in claims
                    ],
                    "routing": {
                        **case_routing_summary(claims),
                        "history": [
                            {
                                "decision_id": item.id,
                                "decision_version": item.decision_version,
                                "claim_id": item.claim_id,
                                "router_id": item.router_id,
                                "router_version": item.router_version,
                                "issue_type": item.issue_type,
                                "claim_type": item.claim_type,
                                "routing_source": item.routing_source,
                                "routing_status": item.routing_status,
                                "confidence": item.confidence,
                                "reason": item.reason,
                                "matched_signals": item.matched_signals_json,
                                "skill_name": item.skill_name,
                                "skill_version": item.skill_version,
                                "requires_human_confirmation": item.requires_human_confirmation,
                                "content_sha256": item.content_sha256,
                                "actor_id": item.actor_id,
                                "created_at": item.created_at,
                            }
                            for item in routing_decisions
                        ],
                    },
                    "listing_snapshot": {
                        "snapshot_id": listing.id,
                        "payload": listing.payload_json,
                        "content_sha256": listing.content_sha256,
                        "captured_at": listing.captured_at,
                    }
                    if listing
                    else None,
                    "evidence": [self._evidence_view(item) for item in evidence],
                    "messages": [
                        {
                            "message_id": item.id,
                            "sender_id": item.sender_id,
                            "sender_role": item.sender_role,
                            "body": item.body,
                            "sent_at": item.sent_at,
                            "content_sha256": item.content_sha256,
                            "snapshot_locked": item.snapshot_locked,
                        }
                        for item in messages
                    ],
                    "policy": {
                        "policy_id": policy.policy_id,
                        "version": policy.version,
                        "effective_from": policy.effective_from,
                        "effective_to": policy.effective_to,
                        "rules": policy.document_json.get("rules", []),
                    }
                    if policy
                    else None,
                    "timeline": self._timeline(transaction, messages, shipment, evidence, events),
                    "evidence_graph": self._evidence_graph(claims, evidence, latest, citation_index),
                    "open_questions": [
                        {
                            "question_id": item.id,
                            "target": item.target,
                            "question": item.question,
                            "missing_fact": item.missing_fact,
                            "status": item.status,
                            "round_number": item.round_number,
                            "resolves_claim_ids": item.resolves_claim_ids_json,
                            "response_evidence_ids": item.response_evidence_ids_json,
                            "deadline": item.deadline,
                        }
                        for item in questions
                    ],
                    "recommendation": {
                        "decision_id": latest.id,
                        "version": latest.version,
                        "status": latest.status,
                        "outcome": latest.outcome,
                        "content_sha256": latest.content_sha256,
                        "payload": latest.payload_json,
                    }
                    if latest
                    else None,
                    "agent_trace": [
                        {
                            "output_id": item.id,
                            "case_run_id": item.case_run_id,
                            "role": item.role,
                            "output_type": item.output_type,
                            "model_name": item.model_name,
                            "prompt_version": item.prompt_version,
                            "content_sha256": item.content_sha256,
                            "usage": item.usage_json,
                            "payload": item.payload_json,
                            "created_at": item.created_at,
                        }
                        for item in outputs
                    ],
                    "runs": [
                        {
                            "case_run_id": item.id,
                            "run_number": item.run_number,
                            "status": item.status,
                            "phase": item.phase,
                            "checkpoint": item.checkpoint_json,
                            "tool_calls_used": item.tool_calls_used,
                            "tokens_used": item.tokens_used,
                        }
                        for item in runs
                    ],
                    "review": {
                        "decisions": [self._decision_view(item) for item in decisions],
                        "guard_reports": [
                            {
                                "guard_result_id": item.id,
                                "decision_id": item.decision_id,
                                "guard_version": item.guard_version,
                                "passed": item.passed,
                                "required_action": item.required_action,
                                "violations": item.violations_json,
                                "checks": item.checks_json,
                                "content_sha256": item.content_sha256,
                                "created_at": item.created_at,
                            }
                            for item in guard_reports
                        ],
                        "approvals": [
                            {
                                "approval_id": item.id,
                                "decision_id": item.decision_id,
                                "reviewer_id": item.reviewer_id,
                                "action": item.action,
                                "reason": item.reason,
                                "created_at": item.created_at,
                            }
                            for item in approvals
                        ],
                    },
                    "execution": {
                        "actions": [
                            {
                                "action_id": item.id,
                                "decision_id": item.decision_id,
                                "action_type": item.action_type,
                                "amount_minor": item.amount_minor,
                                "status": item.status,
                                "idempotency_key": item.idempotency_key,
                                "external_reference": item.external_reference,
                                "failure_code": item.failure_code,
                                "result": item.result_json,
                            }
                            for item in actions
                        ],
                        "transaction_status": {
                            "order_status": transaction.order_status,
                            "funds_status": transaction.funds_status,
                        },
                    },
                    "appeals": [
                        {
                            "appeal_id": item.id,
                            "decision_id": item.decision_id,
                            "appellant_id": item.appellant_id,
                            "appellant_role": item.appellant_role,
                            "recorded_by_id": item.recorded_by_id,
                            "grounds": item.grounds,
                            "statement": item.statement,
                            "evidence_ids": item.evidence_ids_json,
                            "status": item.status,
                            "deadline": item.deadline,
                            "resolved_at": item.resolved_at,
                            "resolution_reason": item.resolution_reason,
                        }
                        for item in appeals
                    ],
                    "events": [
                        {
                            "sequence": item.sequence,
                            "event_type": item.event_type,
                            "from_state": item.from_state,
                            "to_state": item.to_state,
                            "actor_type": item.actor_type,
                            "actor_id": item.actor_id,
                            "reason_code": item.reason_code,
                            "metadata": item.metadata_json,
                            "occurred_at": item.occurred_at,
                        }
                        for item in events
                    ],
                }
            )

    @staticmethod
    def _evidence_view(item: Evidence) -> dict[str, Any]:
        return {
            "evidence_id": item.id,
            "submitted_by": item.submitted_by,
            "recorded_by_id": item.recorded_by_id,
            "evidence_type": item.evidence_type,
            "description": item.description,
            "text_content": item.content_text,
            "source_system": item.source_system,
            "source_record_id": item.source_record_id,
            "captured_at": item.captured_at,
            "submitted_at": item.submitted_at,
            "content_sha256": item.content_sha256,
            "integrity_status": item.integrity_status,
            "related_claim_ids": item.related_claim_ids_json,
            "extracted_facts": item.extracted_facts_json,
            "handling_flags": item.handling_flags_json,
        }

    @staticmethod
    def _decision_view(item: Decision) -> dict[str, Any]:
        return {
            "decision_id": item.id,
            "version": item.version,
            "status": item.status,
            "outcome": item.outcome,
            "case_run_id": item.case_run_id,
            "supersedes_decision_id": item.supersedes_decision_id,
            "content_sha256": item.content_sha256,
            "requires_human_review": item.requires_human_review,
            "created_at": item.created_at,
        }

    @staticmethod
    def _citation_index(outputs: list[AgentOutput]) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for output in outputs:
            if output.role != "EVIDENCE_POLICY_CLERK":
                continue
            for citation in output.payload_json.get("policy_citations", []):
                index[citation.get("citation_id", "")] = citation
        return index

    @classmethod
    def _evidence_graph(
        cls,
        claims: list[Claim],
        evidence: list[Evidence],
        decision: Decision | None,
        citation_index: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = [
            {"id": f"claim:{item.id}", "type": "claim", "label": item.statement, "party": item.party}
            for item in claims
        ]
        nodes.extend(
            {"id": f"evidence:{item.id}", "type": "evidence", "label": item.description, "evidence_type": item.evidence_type}
            for item in evidence
        )
        edges: list[dict[str, Any]] = []
        for item in evidence:
            for claim_id in item.related_claim_ids_json:
                edges.append({"source": f"evidence:{item.id}", "target": f"claim:{claim_id}", "relation": "RELATED_TO"})
        if decision:
            for finding in decision.payload_json.get("claim_findings", []):
                claim_id = finding.get("claim_id")
                for evidence_id in finding.get("evidence_ids", []):
                    edges.append({"source": f"evidence:{evidence_id}", "target": f"claim:{claim_id}", "relation": "CITED_BY_FINDING"})
                for citation_id in finding.get("policy_citation_ids", []):
                    citation = citation_index.get(citation_id)
                    if citation:
                        rule_id = citation.get("rule_id")
                        node_id = f"rule:{rule_id}"
                        if not any(node["id"] == node_id for node in nodes):
                            nodes.append({"id": node_id, "type": "rule", "label": rule_id, "citation_id": citation_id})
                        edges.append({"source": f"claim:{claim_id}", "target": node_id, "relation": "SUPPORTED_BY_RULE"})
        return {"nodes": nodes, "edges": edges}

    @staticmethod
    def _timeline(transaction: Transaction, messages: list[Message], shipment: list[ShipmentEvent], evidence: list[Evidence], events: list[CaseEvent]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = [
            {"at": transaction.paid_at, "type": "TRANSACTION_PAID", "label": "交易支付", "source_id": transaction.id},
        ]
        if transaction.delivered_at:
            entries.append({"at": transaction.delivered_at, "type": "TRANSACTION_DELIVERED", "label": "交易签收", "source_id": transaction.id})
        entries.extend({"at": item.sent_at, "type": "MESSAGE", "label": item.body, "source_id": item.id} for item in messages)
        entries.extend({"at": item.occurred_at, "type": "SHIPMENT", "label": item.event_type, "source_id": item.id} for item in shipment)
        entries.extend({"at": item.submitted_at, "type": "EVIDENCE_SUBMITTED", "label": item.description, "source_id": item.id} for item in evidence)
        entries.extend({"at": item.occurred_at, "type": "CASE_EVENT", "label": f"{item.from_state} → {item.to_state}", "source_id": item.id} for item in events)
        return sorted(entries, key=lambda item: item["at"])
