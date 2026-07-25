from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuestionProposal(StrictModel):
    target: Literal["BUYER", "SELLER"]
    question: str = Field(min_length=1)
    missing_fact: str = Field(min_length=1)
    resolves_claim_ids: list[str] = Field(min_length=1)
    acceptable_evidence_types: list[str] = Field(min_length=1)
    basis_evidence_ids: list[str] = Field(default_factory=list)
    generation_reason: str = "MATERIAL_EVIDENCE_GAP"


class ClaimAssessment(StrictModel):
    claim_id: str
    position: Literal["SUPPORT", "OPPOSE", "PARTIAL", "UNDETERMINED"]
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contrary_evidence_ids: list[str] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    reasoning_summary: str = Field(min_length=1)


class PartyAnalysis(StrictModel):
    analysis_id: str
    case_id: str
    case_run_id: str
    party: Literal["BUYER", "SELLER"]
    analyzed_claim_ids: list[str]
    claim_assessments: list[ClaimAssessment]
    response_points: list[str] = Field(default_factory=list)
    proposed_questions: list[QuestionProposal] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    generated_at: datetime


class EvidenceAssessment(StrictModel):
    assessment_id: str
    evidence_id: str
    usability: Literal["USABLE", "USABLE_WITH_LIMITS", "NOT_USABLE"]
    relevance: Literal["HIGH", "MEDIUM", "LOW", "NONE"]
    authenticity: Literal["VERIFIED", "PLAUSIBLE", "UNVERIFIED", "CONTESTED", "FAILED"]
    reliability: Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
    temporal_fit: Literal["DIRECT", "NEAR_EVENT", "REMOTE", "UNKNOWN"]
    probative_value: Literal["DIRECT", "CORROBORATIVE", "CONTEXT_ONLY", "NONE"]
    findings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    related_claim_ids: list[str] = Field(default_factory=list)


class TimelineEntry(StrictModel):
    timeline_event_id: str
    event_type: str
    occurred_at: datetime
    description: str
    source_evidence_ids: list[str]
    confidence: float = Field(ge=0, le=1)
    disputed: bool


class PolicyCitation(StrictModel):
    citation_id: str
    policy_id: str
    policy_version: str
    rule_id: str
    citation_key: str
    applies_to_claim_ids: list[str]
    rationale: str


class EvidenceConflict(StrictModel):
    conflict_id: str
    description: str
    evidence_ids: list[str] = Field(min_length=2)
    material: bool
    resolution_status: Literal["OPEN", "RESOLVED", "REQUIRES_HUMAN"]
    resolution: str | None = None


class EvidencePolicyReport(StrictModel):
    report_id: str
    case_id: str
    case_run_id: str
    analyzed_claim_ids: list[str]
    policy_id: str
    policy_version: str
    policy_basis_time: datetime
    timeline: list[TimelineEntry]
    evidence_assessments: list[EvidenceAssessment]
    policy_citations: list[PolicyCitation]
    conflicts: list[EvidenceConflict]
    proposed_questions: list[QuestionProposal] = Field(default_factory=list)
    unresolved_issues: list[str] = Field(default_factory=list)
    requires_human_review: bool
    human_review_reasons: list[str] = Field(default_factory=list)
    generated_at: datetime


class EstablishedFact(StrictModel):
    fact_id: str
    statement: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ClaimFinding(StrictModel):
    claim_id: str
    finding: Literal["SUPPORTED", "PARTIALLY_SUPPORTED", "NOT_SUPPORTED", "INSUFFICIENT_EVIDENCE"]
    rationale: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    policy_citation_ids: list[str] = Field(min_length=1)


class ProposedAction(StrictModel):
    action_type: Literal["CREATE_RETURN", "FULL_REFUND", "PARTIAL_REFUND", "RELEASE_FUNDS", "FREEZE_FUNDS"]
    amount_minor: int | None = Field(default=None, ge=0)


class DecisionRecommendation(StrictModel):
    recommendation_id: str
    case_id: str
    case_run_id: str
    outcome: Literal[
        "RETURN_AND_FULL_REFUND",
        "PARTIAL_REFUND",
        "REJECT_CLAIM",
        "REQUEST_MORE_EVIDENCE",
        "RELEASE_FUNDS",
        "ESCALATE_TO_HUMAN",
    ]
    claim_findings: list[ClaimFinding] = Field(min_length=1)
    established_facts: list[EstablishedFact] = Field(default_factory=list)
    refund_amount_minor: int | None = Field(default=None, ge=0)
    currency: Literal["CNY"] = "CNY"
    shipping_payer: Literal["BUYER", "SELLER", "PLATFORM", "NOT_APPLICABLE", "UNDETERMINED"]
    unresolved_question_ids: list[str] = Field(default_factory=list)
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    policy_citation_ids: list[str] = Field(min_length=1)
    requires_human_review: Literal[True] = True
    human_review_reasons: list[str] = Field(min_length=1)
    uncertainty: Literal["LOW", "MEDIUM", "HIGH"]
    reviewer_explanation: str = Field(min_length=1)
    user_explanation: str = Field(min_length=1)
    generated_at: datetime

    @field_validator("established_facts")
    @classmethod
    def facts_require_evidence(cls, value: list[EstablishedFact]) -> list[EstablishedFact]:
        if any(not fact.evidence_ids for fact in value):
            raise ValueError("established facts must cite evidence")
        return value


class AgentResultEnvelope(StrictModel):
    output_id: str
    case_id: str
    case_run_id: str
    role: Literal[
        "BUYER_CASE_ANALYST",
        "SELLER_CASE_ANALYST",
        "EVIDENCE_POLICY_CLERK",
        "ADJUDICATION_AGENT",
    ]
    output_type: Literal["PARTY_ANALYSIS", "EVIDENCE_POLICY_REPORT", "DECISION_RECOMMENDATION"]
    schema_version: str = "1.0.0"
    prompt_version: str
    model_name: str
    input_fingerprint: str
    payload: dict[str, Any]
    usage: dict[str, Any]
    content_sha256: str
    created_at: datetime


class GuardViolation(StrictModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    severity: Literal["BLOCK", "WARN"]
    message: str = Field(min_length=1)
    json_path: str = Field(min_length=1)
    related_rule_id: str | None = None


class DecisionGuardResult(StrictModel):
    guard_result_id: str
    case_id: str
    case_run_id: str
    decision_id: str
    guard_version: str
    passed: bool
    violations: list[GuardViolation]
    checks: dict[str, Any]
    required_action: Literal[
        "PROCEED_TO_HUMAN_REVIEW",
        "RETURN_TO_ADJUDICATION",
        "ESCALATE_TO_HUMAN",
        "BLOCK_EXECUTION",
    ]
    decision_content_sha256: str
    content_sha256: str
    checked_at: datetime
