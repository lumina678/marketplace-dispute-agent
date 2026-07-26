from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dispute_agent.dispute_types import DisputeType, RoutingSource, RoutingStatus


class RoutingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelRoutingCandidate(RoutingModel):
    issue_type: DisputeType
    claim_type: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]+$")
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(default="", max_length=1000)


class ClaimRoutingHint(RoutingModel):
    declared_issue_type: DisputeType | None = None
    declared_claim_type: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]+$")
    platform_reason_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]+$")
    model_candidate: ModelRoutingCandidate | None = None

    @model_validator(mode="after")
    def declared_claim_type_requires_issue_type(self) -> "ClaimRoutingHint":
        if self.declared_claim_type and self.declared_issue_type is None:
            raise ValueError("declared_claim_type 必须与 declared_issue_type 一起提供")
        return self


class RouteClaimRequest(ClaimRoutingHint):
    actor_id: str = Field(default="claim-router-api", min_length=1, max_length=80)
    force_recompute: bool = False


class CaseClaimRoutingHint(ClaimRoutingHint):
    claim_id: str = Field(min_length=1, max_length=80)


class RouteCaseRequest(RoutingModel):
    claim_hints: list[CaseClaimRoutingHint] = Field(default_factory=list)
    actor_id: str = Field(default="claim-router-api", min_length=1, max_length=80)
    force_recompute: bool = False

    @model_validator(mode="after")
    def claim_ids_are_unique(self) -> "RouteCaseRequest":
        claim_ids = [item.claim_id for item in self.claim_hints]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("claim_hints.claim_id 不能重复")
        return self


class RoutingOverrideRequest(RoutingModel):
    reviewer_id: str = Field(min_length=1, max_length=80)
    issue_type: DisputeType
    claim_type: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]+$")
    reason: str = Field(min_length=1, max_length=2000)


class RouterThresholds(RoutingModel):
    minimum_rule_confidence: float = Field(ge=0, le=1)
    ambiguity_margin: float = Field(ge=0, le=1)
    model_candidate_requires_human: Literal[True] = True


class RouteTarget(RoutingModel):
    issue_type: DisputeType
    claim_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    confidence: float = Field(ge=0, le=1)


class KeywordRouteRule(RouteTarget):
    rule_id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]+$")
    phrases: tuple[str, ...] = Field(min_length=1)


class RouterConfig(RoutingModel):
    schema_version: Literal["1.0.0"]
    router_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    thresholds: RouterThresholds
    default_claim_types: dict[str, str]
    platform_reason_codes: dict[str, RouteTarget]
    keyword_rules: tuple[KeywordRouteRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_router_contract(self) -> "RouterConfig":
        expected = {item.value for item in DisputeType}
        if set(self.default_claim_types) != expected:
            raise ValueError("default_claim_types 必须完整覆盖所有 dispute type")
        if any(not value or not value.replace("_", "").isalnum() or value.upper() != value for value in self.default_claim_types.values()):
            raise ValueError("default_claim_types 必须使用大写稳定代码")
        if any(code.upper() != code for code in self.platform_reason_codes):
            raise ValueError("platform reason code 必须为大写稳定代码")
        rule_ids = [item.rule_id for item in self.keyword_rules]
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("keyword_rules.rule_id 不能重复")
        return self


class RoutingDecision(RoutingModel):
    router_id: str
    router_version: str
    issue_type: DisputeType
    claim_type: str
    routing_source: RoutingSource
    routing_status: RoutingStatus
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1)
    matched_signals: list[str] = Field(default_factory=list)
    skill_name: str | None = None
    skill_version: str | None = None
    requires_human_confirmation: bool
