from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dispute_agent.dispute_types import DisputeType


DecisionOutcome = Literal[
    "RETURN_AND_FULL_REFUND",
    "PARTIAL_REFUND",
    "REJECT_CLAIM",
    "REQUEST_MORE_EVIDENCE",
    "RELEASE_FUNDS",
    "ESCALATE_TO_HUMAN",
]


class SkillModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimTypeDefinition(SkillModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)


class SkillEvidenceRequirement(SkillModel):
    requirement_id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]+$")
    description: str = Field(min_length=1)
    applies_to_claim_types: tuple[str, ...] = Field(min_length=1)
    accepted_evidence_types: tuple[str, ...] = Field(min_length=1)
    minimum_independent_sources: int = Field(default=1, ge=1)
    mandatory_for_supported_finding: bool = True


class SkillPolicyScope(SkillModel):
    policy_ids: tuple[str, ...] = Field(min_length=1)
    dispute_types: tuple[DisputeType, ...] = Field(min_length=1)
    selection_basis: Literal["paid_at"] = "paid_at"


class InvestigationStepDefinition(SkillModel):
    step_id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]+$")
    description: str = Field(min_length=1)
    tool_names: tuple[str, ...] = Field(default_factory=tuple)
    required: bool = True


class QuestionRuleDefinition(SkillModel):
    rule_id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]+$")
    target: Literal["BUYER", "SELLER", "CLAIMANT", "RESPONDENT", "EITHER"]
    trigger: str = Field(min_length=1)
    question_template: str = Field(min_length=1)
    applies_to_claim_types: tuple[str, ...] = Field(min_length=1)
    acceptable_evidence_types: tuple[str, ...] = Field(min_length=1)


class SkillGuardProfile(SkillModel):
    human_review_required: bool = True
    require_policy_citations: bool = True
    require_evidence_for_established_facts: bool = True
    automatic_final_decision_allowed: bool = False
    allowed_outcomes: tuple[DecisionOutcome, ...] = Field(min_length=1)
    mandatory_escalation_conditions: tuple[str, ...] = Field(default_factory=tuple)


class SkillManifest(SkillModel):
    """Serializable, versioned contract for one dispute investigation skill."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    name: str = Field(pattern=r"^[a-z][a-z0-9-]+$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    dispute_types: tuple[DisputeType, ...] = Field(min_length=1)
    claim_types: tuple[ClaimTypeDefinition, ...] = Field(min_length=1)
    required_context: tuple[str, ...] = Field(min_length=1)
    allowed_tools: tuple[str, ...] = Field(min_length=1)
    evidence_requirements: tuple[SkillEvidenceRequirement, ...] = Field(min_length=1)
    policy_scope: SkillPolicyScope
    investigation_steps: tuple[InvestigationStepDefinition, ...] = Field(min_length=1)
    question_rules: tuple[QuestionRuleDefinition, ...] = Field(min_length=1)
    allowed_outcomes: tuple[DecisionOutcome, ...] = Field(min_length=1)
    guard_profile: SkillGuardProfile

    @model_validator(mode="after")
    def validate_internal_references(self) -> "SkillManifest":
        claim_type_codes = {item.code for item in self.claim_types}
        if len(claim_type_codes) != len(self.claim_types):
            raise ValueError("claim_types.code 不能重复")
        requirement_ids = {item.requirement_id for item in self.evidence_requirements}
        if len(requirement_ids) != len(self.evidence_requirements):
            raise ValueError("evidence_requirements.requirement_id 不能重复")
        step_ids = {item.step_id for item in self.investigation_steps}
        if len(step_ids) != len(self.investigation_steps):
            raise ValueError("investigation_steps.step_id 不能重复")
        question_ids = {item.rule_id for item in self.question_rules}
        if len(question_ids) != len(self.question_rules):
            raise ValueError("question_rules.rule_id 不能重复")

        for requirement in self.evidence_requirements:
            if not set(requirement.applies_to_claim_types).issubset(claim_type_codes):
                raise ValueError(f"{requirement.requirement_id} 引用了未知 claim_type")
        for rule in self.question_rules:
            if not set(rule.applies_to_claim_types).issubset(claim_type_codes):
                raise ValueError(f"{rule.rule_id} 引用了未知 claim_type")
        for step in self.investigation_steps:
            if not set(step.tool_names).issubset(self.allowed_tools):
                raise ValueError(f"{step.step_id} 使用了 Skill 未授权的工具")

        if set(self.dispute_types) != set(self.policy_scope.dispute_types):
            raise ValueError("policy_scope.dispute_types 必须与 Skill dispute_types 一致")
        if set(self.allowed_outcomes) != set(self.guard_profile.allowed_outcomes):
            raise ValueError("guard_profile.allowed_outcomes 必须与 Skill allowed_outcomes 一致")
        if len(set(self.required_context)) != len(self.required_context):
            raise ValueError("required_context 不能重复")
        if len(set(self.allowed_tools)) != len(self.allowed_tools):
            raise ValueError("allowed_tools 不能重复")
        return self


class DisputeSkill(ABC):
    """Application-level investigation playbook, not a free-form prompt."""

    @property
    @abstractmethod
    def manifest(self) -> SkillManifest:
        raise NotImplementedError

    def supports(self, dispute_type: str, claim_type: str | None = None) -> bool:
        if dispute_type not in {item.value for item in self.manifest.dispute_types}:
            return False
        if claim_type is None:
            return True
        return claim_type in {item.code for item in self.manifest.claim_types}

    def required_context(self) -> tuple[str, ...]:
        return self.manifest.required_context

    def claim_schema(self) -> tuple[ClaimTypeDefinition, ...]:
        return self.manifest.claim_types

    def evidence_requirements(self) -> tuple[SkillEvidenceRequirement, ...]:
        return self.manifest.evidence_requirements

    def policy_scope(self) -> SkillPolicyScope:
        return self.manifest.policy_scope

    def allowed_tools(self) -> tuple[str, ...]:
        return self.manifest.allowed_tools

    def investigation_steps(self) -> tuple[InvestigationStepDefinition, ...]:
        return self.manifest.investigation_steps

    def question_rules(self) -> tuple[QuestionRuleDefinition, ...]:
        return self.manifest.question_rules

    def allowed_outcomes(self) -> tuple[DecisionOutcome, ...]:
        return self.manifest.allowed_outcomes

    def guard_profile(self) -> SkillGuardProfile:
        return self.manifest.guard_profile


class ManifestDisputeSkill(DisputeSkill):
    def __init__(self, manifest: SkillManifest):
        self._manifest = manifest

    @property
    def manifest(self) -> SkillManifest:
        return self._manifest

