from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.config import Settings, get_settings
from dispute_agent.db import SessionLocal
from dispute_agent.dispute_types import DisputeType, RoutingSource, RoutingStatus
from dispute_agent.errors import AuthorizationError, ConflictError, NotFoundError, ValidationError
from dispute_agent.ids import new_id
from dispute_agent.models import Claim, ClaimRoutingDecision, Dispute, User, utc_now
from dispute_agent.routing_schemas import ClaimRoutingHint, RouterConfig, RoutingDecision
from dispute_agent.serialization import content_hash
from dispute_agent.services.claim_routing import claim_routing_payload, case_routing_summary
from dispute_agent.skills import SkillRegistry, get_skill_registry


ROUTING_MUTABLE_STATES = {"SUBMITTED", "REOPENED"}


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


class DeterministicClaimRouter:
    """Pure, versioned classifier. It does not write data or call a model."""

    def __init__(self, config_path: Path, *, skill_registry: SkillRegistry | None = None):
        self.config_path = Path(config_path)
        self.skill_registry = skill_registry or get_skill_registry()
        try:
            with self.config_path.open("r", encoding="utf-8") as handle:
                self.config = RouterConfig.model_validate(json.load(handle))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValidationError(f"无法加载 Router 配置 {self.config_path}: {exc}") from exc
        self._validate_skill_bindings()

    def decide(
        self,
        *,
        statement: str,
        current_claim_type: str,
        hint: ClaimRoutingHint | None = None,
    ) -> RoutingDecision:
        hint = hint or ClaimRoutingHint()
        if hint.declared_issue_type is not None:
            claim_type = self._claim_type_for(
                hint.declared_issue_type,
                requested=hint.declared_claim_type,
                current=current_claim_type,
            )
            return self._finalize(
                issue_type=hint.declared_issue_type,
                claim_type=claim_type,
                source=RoutingSource.USER_DECLARED,
                confidence=1.0,
                reason="使用提交方明确声明的争议类型；声明优先于平台原因码、文本规则和模型候选。",
                matched_signals=[f"declared_issue_type:{hint.declared_issue_type.value}"],
            )

        unmapped_platform_signal: str | None = None
        if hint.platform_reason_code:
            target = self.config.platform_reason_codes.get(hint.platform_reason_code)
            if target is not None:
                return self._finalize(
                    issue_type=target.issue_type,
                    claim_type=target.claim_type,
                    source=RoutingSource.PLATFORM_REASON_CODE,
                    confidence=target.confidence,
                    reason=f"平台结构化原因码 {hint.platform_reason_code} 映射到固定争议类型。",
                    matched_signals=[f"platform_reason_code:{hint.platform_reason_code}"],
                )
            unmapped_platform_signal = f"unmapped_platform_reason_code:{hint.platform_reason_code}"

        normalized = _normalized_text(statement)
        candidates: dict[DisputeType, list[tuple[float, str, str, list[str]]]] = defaultdict(list)
        for rule in self.config.keyword_rules:
            matches = [phrase for phrase in rule.phrases if _normalized_text(phrase) in normalized]
            if not matches:
                continue
            score = min(1.0, rule.confidence + max(0, len(matches) - 1) * 0.01)
            candidates[rule.issue_type].append((score, rule.claim_type, rule.rule_id, matches))

        ranked: list[tuple[float, DisputeType, str, str, list[str]]] = []
        for issue_type, matches in candidates.items():
            score, claim_type, rule_id, phrases = max(matches, key=lambda item: item[0])
            ranked.append((score, issue_type, claim_type, rule_id, phrases))
        ranked.sort(key=lambda item: (-item[0], item[1].value))

        if ranked:
            top_score, issue_type, claim_type, rule_id, phrases = ranked[0]
            signals = [f"keyword:{rule_id}:{phrase}" for phrase in phrases]
            if unmapped_platform_signal:
                signals.insert(0, unmapped_platform_signal)
            ambiguous = len(ranked) > 1 and top_score - ranked[1][0] < self.config.thresholds.ambiguity_margin
            if ambiguous:
                second = ranked[1]
                signals.append(f"competing_issue_type:{second[1].value}:{second[0]:.2f}")
                return self._finalize(
                    issue_type=issue_type,
                    claim_type=claim_type,
                    source=RoutingSource.DETERMINISTIC_RULE,
                    confidence=top_score,
                    reason=(
                        f"文本同时命中 {issue_type.value} 和 {second[1].value}，"
                        f"分差 {top_score - second[0]:.2f} 小于歧义阈值，必须人工拆分或确认主张。"
                    ),
                    matched_signals=signals,
                    force_human=True,
                )
            return self._finalize(
                issue_type=issue_type,
                claim_type=claim_type,
                source=RoutingSource.DETERMINISTIC_RULE,
                confidence=top_score,
                reason=f"文本命中确定性路由规则 {rule_id}。",
                matched_signals=signals,
            )

        if hint.model_candidate is not None:
            candidate = hint.model_candidate
            claim_type = self._claim_type_for(
                candidate.issue_type,
                requested=candidate.claim_type,
                current=current_claim_type,
            )
            signals = [f"model_candidate:{candidate.issue_type.value}:{candidate.confidence:.2f}"]
            if unmapped_platform_signal:
                signals.insert(0, unmapped_platform_signal)
            return self._finalize(
                issue_type=candidate.issue_type,
                claim_type=claim_type,
                source=RoutingSource.MODEL_SUGGESTION,
                confidence=candidate.confidence,
                reason="没有可用的明确声明、平台映射或确定性文本规则；模型候选仅供人工确认。",
                matched_signals=signals,
                force_human=self.config.thresholds.model_candidate_requires_human,
            )

        signals = [unmapped_platform_signal] if unmapped_platform_signal else []
        return self._finalize(
            issue_type=DisputeType.OTHER,
            claim_type=self.config.default_claim_types[DisputeType.OTHER.value],
            source=RoutingSource.DETERMINISTIC_RULE,
            confidence=0.0,
            reason="没有命中明确声明、平台原因码或确定性文本规则，必须人工分类。",
            matched_signals=signals,
            force_human=True,
        )

    def override_decision(self, *, issue_type: DisputeType, claim_type: str | None, reason: str) -> RoutingDecision:
        selected_claim_type = self._claim_type_for(issue_type, requested=claim_type, current="OTHER")
        return self._finalize(
            issue_type=issue_type,
            claim_type=selected_claim_type,
            source=RoutingSource.REVIEWER_OVERRIDE,
            confidence=1.0,
            reason=f"审核员覆盖：{reason.strip()}",
            matched_signals=[f"reviewer_override:{issue_type.value}"],
            override=True,
        )

    def _claim_type_for(self, issue_type: DisputeType, *, requested: str | None, current: str) -> str:
        skills = self.skill_registry.for_dispute_type(issue_type.value)
        if requested:
            if skills and not any(skill.supports(issue_type.value, requested) for skill in skills):
                raise ValidationError(f"claim_type={requested} 不受 {issue_type.value} 的已注册 Skill 支持")
            return requested
        if skills and any(skill.supports(issue_type.value, current) for skill in skills):
            return current
        return self.config.default_claim_types[issue_type.value]

    def _finalize(
        self,
        *,
        issue_type: DisputeType,
        claim_type: str,
        source: RoutingSource,
        confidence: float,
        reason: str,
        matched_signals: list[str],
        force_human: bool = False,
        override: bool = False,
    ) -> RoutingDecision:
        skills = self.skill_registry.for_dispute_type(issue_type.value)
        compatible = [skill for skill in skills if skill.supports(issue_type.value, claim_type)]
        if len(compatible) > 1:
            raise ValidationError(f"{issue_type.value}/{claim_type} 匹配多个 Skill，必须先消除注册表歧义")
        skill = compatible[0] if compatible else None
        requires_human = (
            force_human
            or confidence < self.config.thresholds.minimum_rule_confidence
            or issue_type in {DisputeType.COUNTERFEIT, DisputeType.OTHER}
            or skill is None
        )
        status = RoutingStatus.NEEDS_HUMAN if requires_human else RoutingStatus.OVERRIDDEN if override else RoutingStatus.ROUTED
        if skill is None and issue_type not in {DisputeType.COUNTERFEIT, DisputeType.OTHER}:
            reason = f"{reason} 当前没有兼容的已注册 Skill，必须人工处理。"
        return RoutingDecision(
            router_id=self.config.router_id,
            router_version=self.config.version,
            issue_type=issue_type,
            claim_type=claim_type,
            routing_source=source,
            routing_status=status,
            confidence=confidence,
            reason=reason,
            matched_signals=matched_signals,
            skill_name=skill.manifest.name if skill else None,
            skill_version=skill.manifest.version if skill else None,
            requires_human_confirmation=requires_human,
        )

    def _validate_skill_bindings(self) -> None:
        targets = [
            (target.issue_type, target.claim_type)
            for target in [*self.config.platform_reason_codes.values(), *self.config.keyword_rules]
        ]
        targets.extend(
            (DisputeType(issue_type_value), claim_type)
            for issue_type_value, claim_type in self.config.default_claim_types.items()
        )
        for issue_type, claim_type in targets:
            if issue_type in {DisputeType.COUNTERFEIT, DisputeType.OTHER}:
                continue
            skills = self.skill_registry.for_dispute_type(issue_type.value)
            if len(skills) != 1:
                raise ValidationError(f"Router 要求 {issue_type.value} 唯一绑定一个 Skill，实际为 {len(skills)}")
            if not skills[0].supports(issue_type.value, claim_type):
                raise ValidationError(
                    f"Router claim_type={claim_type} 不受 {skills[0].manifest.name}@{skills[0].manifest.version} 支持"
                )


class ClaimRoutingService:
    """Transactional routing, audit history, idempotency and reviewer override."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        settings: Settings | None = None,
        router: DeterministicClaimRouter | None = None,
        skill_registry: SkillRegistry | None = None,
    ):
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.skill_registry = skill_registry or get_skill_registry()
        self.router = router or DeterministicClaimRouter(
            self.settings.router_config_path,
            skill_registry=self.skill_registry,
        )

    def route_claim(
        self,
        case_id: str,
        claim_id: str,
        *,
        hint: ClaimRoutingHint | None = None,
        actor_id: str = "claim-router",
        force_recompute: bool = False,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute, claim = self._context(session, case_id, claim_id)
            self._require_mutable(dispute)
            result = self._route_claim_in_session(
                session,
                dispute,
                claim,
                hint=hint,
                actor_id=actor_id,
                force_recompute=force_recompute,
            )
            summary = self._update_case_summary(session, dispute)
            session.commit()
            return {"route": result, "case_routing": summary}

    def route_case(
        self,
        case_id: str,
        *,
        hints: dict[str, ClaimRoutingHint] | None = None,
        actor_id: str = "claim-router",
        force_recompute: bool = False,
    ) -> dict[str, Any]:
        hints = hints or {}
        with self.session_factory() as session:
            dispute = session.scalar(select(Dispute).where(Dispute.id == case_id).with_for_update())
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            self._require_mutable(dispute)
            claims = list(
                session.scalars(
                    select(Claim)
                    .where(Claim.dispute_id == case_id)
                    .order_by(Claim.asserted_at)
                    .with_for_update()
                )
            )
            unknown = set(hints) - {item.id for item in claims}
            if unknown:
                raise ValidationError(f"claim_hints 引用了非当前案件 Claim: {sorted(unknown)}")
            routes = [
                self._route_claim_in_session(
                    session,
                    dispute,
                    claim,
                    hint=hints.get(claim.id),
                    actor_id=actor_id,
                    force_recompute=force_recompute,
                )
                for claim in claims
            ]
            summary = self._update_case_summary(session, dispute)
            session.commit()
            return {"case_id": case_id, "routes": routes, "case_routing": summary}

    def override_claim(
        self,
        case_id: str,
        claim_id: str,
        *,
        reviewer_id: str,
        issue_type: DisputeType,
        claim_type: str | None,
        reason: str,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute, claim = self._context(session, case_id, claim_id)
            self._require_mutable(dispute)
            reviewer = session.get(User, reviewer_id)
            if reviewer is None or reviewer.role not in {"REVIEWER", "ADMIN"}:
                raise AuthorizationError("只有已登记的 REVIEWER 或 ADMIN 可以覆盖 Claim 路由")
            decision = self.router.override_decision(issue_type=issue_type, claim_type=claim_type, reason=reason)
            result = self._persist_decision(
                session,
                dispute,
                claim,
                decision,
                actor_id=reviewer_id,
                input_payload={
                    "operation": "REVIEWER_OVERRIDE",
                    "issue_type": issue_type.value,
                    "claim_type": claim_type,
                    "reason": reason.strip(),
                },
            )
            summary = self._update_case_summary(session, dispute)
            session.commit()
            return {"route": result, "case_routing": summary}

    def ensure_case_routed_in_session(
        self,
        session: Session,
        dispute: Dispute,
        *,
        actor_id: str = "orchestrator",
    ) -> dict[str, Any]:
        claims = list(session.scalars(select(Claim).where(Claim.dispute_id == dispute.id).order_by(Claim.asserted_at)))
        for claim in claims:
            if claim.routing_status == RoutingStatus.UNROUTED.value:
                self._route_claim_in_session(
                    session,
                    dispute,
                    claim,
                    hint=None,
                    actor_id=actor_id,
                    force_recompute=True,
                )
        return self._update_case_summary(session, dispute)

    def get_case_routing(self, case_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            claims = list(session.scalars(select(Claim).where(Claim.dispute_id == case_id).order_by(Claim.asserted_at)))
            decisions = list(
                session.scalars(
                    select(ClaimRoutingDecision)
                    .where(ClaimRoutingDecision.dispute_id == case_id)
                    .order_by(ClaimRoutingDecision.claim_id, ClaimRoutingDecision.decision_version)
                )
            )
            return {
                "case_id": case_id,
                "router": {
                    "router_id": self.router.config.router_id,
                    "version": self.router.config.version,
                },
                "case_routing": case_routing_summary(claims),
                "claims": [{"claim_id": item.id, "claim_type": item.claim_type, **claim_routing_payload(item)} for item in claims],
                "history": [self._decision_view(item) for item in decisions],
            }

    def policy_id_for_summary(self, summary: dict[str, Any]) -> str:
        if not summary["ready_for_investigation"]:
            raise ConflictError("案件仍有未确认或复合 Skill 路由，不能启动 Agent 调查")
        skill_name, skill_version = summary["skill_bindings"][0].split("@", 1)
        skill = self.skill_registry.get(skill_name, skill_version)
        policy_ids = skill.policy_scope().policy_ids
        if len(policy_ids) != 1:
            raise ConflictError(f"当前编排要求一个 Skill 唯一绑定一个政策族，实际为 {len(policy_ids)}")
        return policy_ids[0]

    def _route_claim_in_session(
        self,
        session: Session,
        dispute: Dispute,
        claim: Claim,
        *,
        hint: ClaimRoutingHint | None,
        actor_id: str,
        force_recompute: bool,
    ) -> dict[str, Any]:
        if not force_recompute and claim.routing_status in {RoutingStatus.ROUTED.value, RoutingStatus.OVERRIDDEN.value}:
            return {"decision_id": None, "reused_current_binding": True, "claim_id": claim.id, **claim_routing_payload(claim)}
        if (
            hint is None
            and claim.routing_source == RoutingSource.USER_DECLARED.value
            and claim.issue_type not in {DisputeType.OTHER.value}
        ):
            # Generic intake persists the declaration before writing the routing
            # audit record. If a process stops between those two commits, replay
            # the declared type instead of reclassifying solely from free text.
            hint = ClaimRoutingHint(
                declared_issue_type=DisputeType(claim.issue_type),
                declared_claim_type=claim.claim_type,
            )
        decision = self.router.decide(statement=claim.statement, current_claim_type=claim.claim_type, hint=hint)
        return self._persist_decision(
            session,
            dispute,
            claim,
            decision,
            actor_id=actor_id,
            input_payload={
                "operation": "ROUTE",
                "hint": hint.model_dump(mode="json") if hint else {},
                "statement_sha256": content_hash(claim.statement),
            },
        )

    def _persist_decision(
        self,
        session: Session,
        dispute: Dispute,
        claim: Claim,
        decision: RoutingDecision,
        *,
        actor_id: str,
        input_payload: dict[str, Any],
    ) -> dict[str, Any]:
        fingerprint = content_hash(
            {
                "router_id": decision.router_id,
                "router_version": decision.router_version,
                "claim_id": claim.id,
                "input": input_payload,
            }
        )
        existing = session.scalar(
            select(ClaimRoutingDecision).where(
                ClaimRoutingDecision.claim_id == claim.id,
                ClaimRoutingDecision.input_fingerprint == fingerprint,
            )
        )
        if existing is not None:
            self._apply_to_claim(claim, existing)
            return {**self._decision_view(existing), "reused_current_binding": True}

        version = (
            session.scalar(
                select(func.max(ClaimRoutingDecision.decision_version)).where(ClaimRoutingDecision.claim_id == claim.id)
            )
            or 0
        ) + 1
        payload = decision.model_dump(mode="json")
        record = ClaimRoutingDecision(
            id=new_id("route"),
            dispute_id=dispute.id,
            claim_id=claim.id,
            case_run_id=dispute.active_case_run_id,
            decision_version=version,
            router_id=decision.router_id,
            router_version=decision.router_version,
            issue_type=decision.issue_type.value,
            claim_type=decision.claim_type,
            routing_source=decision.routing_source.value,
            routing_status=decision.routing_status.value,
            confidence=decision.confidence,
            reason=decision.reason,
            matched_signals_json=decision.matched_signals,
            skill_name=decision.skill_name,
            skill_version=decision.skill_version,
            requires_human_confirmation=decision.requires_human_confirmation,
            input_fingerprint=fingerprint,
            content_sha256=content_hash(payload),
            actor_id=actor_id,
            created_at=utc_now(),
        )
        session.add(record)
        session.flush()
        self._apply_to_claim(claim, record)
        return {**self._decision_view(record), "reused_current_binding": False}

    @staticmethod
    def _apply_to_claim(claim: Claim, decision: ClaimRoutingDecision) -> None:
        claim.issue_type = decision.issue_type
        claim.issue_subtype = decision.claim_type
        claim.claim_type = decision.claim_type
        claim.routing_source = decision.routing_source
        claim.routing_reason = decision.reason
        claim.routing_confidence = decision.confidence
        claim.skill_name = decision.skill_name
        claim.skill_version = decision.skill_version
        claim.routing_status = decision.routing_status

    def _update_case_summary(self, session: Session, dispute: Dispute) -> dict[str, Any]:
        session.flush()
        claims = list(session.scalars(select(Claim).where(Claim.dispute_id == dispute.id).order_by(Claim.asserted_at)))
        self._validate_bound_claims(claims)
        summary = case_routing_summary(claims)
        dispute.dispute_type = summary["primary_issue_type"] or DisputeType.OTHER.value
        reasons = set(dispute.human_review_reasons_json or [])
        reasons.discard("ROUTING_REQUIRES_HUMAN")
        reasons.discard("COMPOUND_SKILL_ROUTING_REQUIRES_HUMAN")
        if summary["unresolved_claim_ids"]:
            reasons.add("ROUTING_REQUIRES_HUMAN")
        if summary["compound"]:
            reasons.add("COMPOUND_SKILL_ROUTING_REQUIRES_HUMAN")
        dispute.human_review_reasons_json = sorted(reasons)
        return summary

    def _validate_bound_claims(self, claims: list[Claim]) -> None:
        for claim in claims:
            if claim.routing_status not in {RoutingStatus.ROUTED.value, RoutingStatus.OVERRIDDEN.value}:
                continue
            skill = self.skill_registry.resolve_bound_skill(name=claim.skill_name, version=claim.skill_version)
            if skill is None or not skill.supports(claim.issue_type, claim.claim_type):
                raise ConflictError(
                    f"Claim {claim.id} 的持久化路由与 Skill Registry 不兼容，必须重新路由或人工修复"
                )

    @staticmethod
    def _context(session: Session, case_id: str, claim_id: str) -> tuple[Dispute, Claim]:
        dispute = session.scalar(select(Dispute).where(Dispute.id == case_id).with_for_update())
        claim = session.scalar(select(Claim).where(Claim.id == claim_id).with_for_update())
        if dispute is None or claim is None or claim.dispute_id != case_id:
            raise NotFoundError("案件或 Claim 不存在")
        return dispute, claim

    @staticmethod
    def _require_mutable(dispute: Dispute) -> None:
        if dispute.state not in ROUTING_MUTABLE_STATES or dispute.active_case_run_id is not None:
            raise ConflictError("活动调查期间禁止原地修改路由；请退回调查或创建新的 Case Run")

    @staticmethod
    def _decision_view(item: ClaimRoutingDecision) -> dict[str, Any]:
        return {
            "decision_id": item.id,
            "decision_version": item.decision_version,
            "case_id": item.dispute_id,
            "claim_id": item.claim_id,
            "case_run_id": item.case_run_id,
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
            "input_fingerprint": item.input_fingerprint,
            "content_sha256": item.content_sha256,
            "actor_id": item.actor_id,
            "created_at": item.created_at,
        }
