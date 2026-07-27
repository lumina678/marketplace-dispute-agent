from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.agent_schemas import DecisionGuardResult, GuardViolation
from dispute_agent.db import SessionLocal
from dispute_agent.errors import ConflictError, NotFoundError, ValidationError
from dispute_agent.ids import new_id
from dispute_agent.models import (
    AgentOutput,
    CaseRun,
    Claim,
    Decision,
    DecisionGuardReport,
    Dispute,
    Evidence,
    OpenQuestion,
    ResolutionAction,
    utc_now,
)
from dispute_agent.serialization import content_hash, jsonable
from dispute_agent.services.policy import PolicyService
from dispute_agent.skills import SkillRegistry, get_skill_registry


GUARD_VERSION = "2.0.0"


class DecisionGuard:
    """Deterministic checks between an Agent recommendation and human review."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        skill_registry: SkillRegistry | None = None,
    ):
        self.session_factory = session_factory
        self.skill_registry = skill_registry or get_skill_registry()

    def evaluate(
        self,
        case_id: str,
        case_run_id: str,
        *,
        decision_id: str | None = None,
    ) -> DecisionGuardResult:
        with self.session_factory() as session:
            dispute = session.get(Dispute, case_id)
            run = session.get(CaseRun, case_run_id)
            if dispute is None or run is None or run.dispute_id != case_id:
                raise NotFoundError("案件或 case_run 不存在")
            if dispute.active_case_run_id != run.id:
                raise ConflictError("Guard 只能检查当前活动 case_run")
            decision_id = decision_id or run.checkpoint_json.get("decision_id")
            decision = session.get(Decision, decision_id) if decision_id else None
            if decision is None or decision.dispute_id != case_id or decision.case_run_id != run.id:
                raise ValidationError("Guard 必须引用当前 run 的决定草稿")

            existing = session.scalar(
                select(DecisionGuardReport).where(DecisionGuardReport.decision_id == decision.id)
            )
            if existing is not None:
                if existing.decision_content_sha256 != decision.content_sha256:
                    raise ConflictError("决定在 Guard 检查后发生变化")
                return self._result(existing)
            if dispute.state != "UNDER_INVESTIGATION" or run.phase != "GUARD_CHECK":
                raise ConflictError("案件当前不在 GUARD_CHECK 阶段")

            violations: list[GuardViolation] = []
            checks: dict[str, Any] = {}

            def violation(
                code: str,
                message: str,
                path: str,
                *,
                severity: str = "BLOCK",
                rule_id: str | None = None,
            ) -> None:
                violations.append(
                    GuardViolation(
                        code=code,
                        severity=severity,  # type: ignore[arg-type]
                        message=message,
                        json_path=path,
                        related_rule_id=rule_id,
                    )
                )

            payload = decision.payload_json
            recomputed_hash = content_hash(payload)
            checks["decision_hash_matches"] = recomputed_hash == decision.content_sha256
            if not checks["decision_hash_matches"]:
                violation("DECISION_HASH_MISMATCH", "决定内容哈希与已保存哈希不一致。", "$.content_sha256")
            if decision.status != "DRAFT":
                violation("DECISION_NOT_DRAFT", "Guard 只能检查尚未审核的决定草稿。", "$.status")

            source_output = session.get(AgentOutput, decision.agent_output_id) if decision.agent_output_id else None
            source_meta = payload.get("source_agent_output") or {}
            source_valid = bool(
                source_output
                and source_output.dispute_id == case_id
                and source_output.case_run_id == run.id
                and source_output.role == "ADJUDICATION_AGENT"
                and source_output.output_type == "DECISION_RECOMMENDATION"
                and source_meta.get("output_id") == source_output.id
                and source_meta.get("content_sha256") == source_output.content_sha256
                and content_hash(source_output.payload_json) == source_output.content_sha256
            )
            checks["source_agent_output_valid"] = source_valid
            if not source_valid:
                violation(
                    "SOURCE_AGENT_OUTPUT_INVALID",
                    "决定没有绑定当前 run 的完整裁决 Agent 输出，或输出哈希不一致。",
                    "$.source_agent_output",
                )
            elif source_output is not None:
                mismatched_fields = [
                    key
                    for key, value in source_output.payload_json.items()
                    if payload.get(key) != value
                ]
                checks["source_payload_mismatched_fields"] = mismatched_fields
                if mismatched_fields:
                    violation(
                        "SOURCE_PAYLOAD_MISMATCH",
                        f"决定草稿与裁决 Agent 输出不一致：{', '.join(mismatched_fields)}。",
                        "$.payload",
                    )

            if not dispute.policy_id or not dispute.policy_version:
                violation("POLICY_NOT_PINNED", "案件没有固定政策版本。", "$.policy")
                policy = None
            else:
                policy = PolicyService(session).get_version(dispute.policy_id, dispute.policy_version)
                paid_at = dispute.transaction.paid_at
                effective = policy.effective_from <= paid_at and (
                    policy.effective_to is None or paid_at < policy.effective_to
                )
                checks["policy_effective_at_paid_at"] = effective
                checks["policy_payload_matches"] = payload.get("policy") == {
                    "policy_id": dispute.policy_id,
                    "version": dispute.policy_version,
                }
                if not effective:
                    violation("POLICY_VERSION_NOT_EFFECTIVE", "固定政策版本在支付时点并未生效。", "$.policy")
                if not checks["policy_payload_matches"]:
                    violation("POLICY_VERSION_MISMATCH", "草稿引用的政策版本与案件固定版本不一致。", "$.policy")
                if decision.outcome not in set(policy.document_json["authority"]["allowed_outcomes"]):
                    violation("OUTCOME_NOT_ALLOWED", "固定政策版本不允许该处置结果。", "$.outcome")

            open_question_count = session.query(OpenQuestion).filter_by(dispute_id=case_id, status="OPEN").count()
            checks["open_question_count"] = open_question_count
            if open_question_count:
                violation("BLOCKING_OPEN_QUESTION", "仍有未回答的阻塞性补问。", "$.open_questions")

            claims = list(session.scalars(select(Claim).where(Claim.dispute_id == case_id)))
            material_claims = [item for item in claims if item.material]
            bound_skill_keys = {
                (item.skill_name, item.skill_version)
                for item in material_claims
                if item.skill_name and item.skill_version
            }
            skill = None
            if len(bound_skill_keys) != 1 or any(
                not item.skill_name or not item.skill_version for item in material_claims
            ):
                violation(
                    "SKILL_BINDING_INVALID",
                    "Guard 要求所有关键主张绑定同一个已确认 Skill 版本。",
                    "$.claim_skill_bindings",
                )
            else:
                skill_name, skill_version = next(iter(bound_skill_keys))
                try:
                    skill = self.skill_registry.get(str(skill_name), str(skill_version))
                except NotFoundError:
                    violation(
                        "SKILL_BINDING_INVALID",
                        f"关键主张绑定了未注册 Skill：{skill_name}@{skill_version}。",
                        "$.claim_skill_bindings",
                    )
                if skill is not None:
                    profile = skill.guard_profile()
                    checks["skill_name"] = skill.manifest.name
                    checks["skill_version"] = skill.manifest.version
                    checks["skill_guard_profile"] = profile.model_dump(mode="json")
                    checks["skill_allowed_outcomes"] = list(skill.allowed_outcomes())
                    if decision.outcome not in set(skill.allowed_outcomes()):
                        violation(
                            "SKILL_OUTCOME_NOT_ALLOWED",
                            "当前 Skill Guard Profile 不允许该处置结果。",
                            "$.outcome",
                        )
                    if dispute.policy_id not in set(skill.policy_scope().policy_ids):
                        violation(
                            "SKILL_POLICY_SCOPE_MISMATCH",
                            "案件固定政策不属于当前 Skill 的 policy_scope。",
                            "$.policy",
                        )
            material_claim_ids = {item.id for item in claims if item.material}
            findings = payload.get("claim_findings", [])
            finding_ids = [item.get("claim_id") for item in findings if isinstance(item, dict)]
            counts = Counter(finding_ids)
            checks["material_claim_ids"] = sorted(material_claim_ids)
            checks["finding_claim_ids"] = sorted(item for item in finding_ids if item)
            missing_claim_ids = material_claim_ids - set(finding_ids)
            unknown_claim_ids = set(finding_ids) - {item.id for item in claims}
            duplicate_claim_ids = {item for item, count in counts.items() if item and count > 1}
            if missing_claim_ids:
                violation(
                    "MATERIAL_CLAIM_OMITTED",
                    f"缺少关键主张认定：{', '.join(sorted(missing_claim_ids))}。",
                    "$.claim_findings",
                )
            if unknown_claim_ids:
                violation("UNKNOWN_CLAIM_REFERENCE", "主张认定引用了其他案件或不存在的主张。", "$.claim_findings")
            if duplicate_claim_ids:
                violation("DUPLICATE_CLAIM_FINDING", "同一主张出现多个认定结果。", "$.claim_findings")

            evidence_ids = set(session.scalars(select(Evidence.id).where(Evidence.dispute_id == case_id)))
            reports = list(
                session.scalars(
                    select(AgentOutput).where(
                        AgentOutput.dispute_id == case_id,
                        AgentOutput.role == "EVIDENCE_POLICY_CLERK",
                        AgentOutput.output_type == "EVIDENCE_POLICY_REPORT",
                    )
                )
            )
            citation_index: dict[str, dict[str, Any]] = {}
            for report in reports:
                for citation in report.payload_json.get("policy_citations", []):
                    citation_index[citation["citation_id"]] = citation

            for index, fact in enumerate(payload.get("established_facts", [])):
                refs = set(fact.get("evidence_ids", []))
                if not refs and (skill is None or skill.guard_profile().require_evidence_for_established_facts):
                    violation(
                        "ESTABLISHED_FACT_WITHOUT_EVIDENCE",
                        "已认定事实必须引用至少一项证据。",
                        f"$.established_facts[{index}].evidence_ids",
                    )
                if not refs.issubset(evidence_ids):
                    violation(
                        "UNKNOWN_EVIDENCE_REFERENCE",
                        "已认定事实引用了其他案件或不存在的证据。",
                        f"$.established_facts[{index}].evidence_ids",
                    )
                statement = str(fact.get("statement", ""))
                if "买方调包" in statement or "买方更换硬件" in statement:
                    violation(
                        "UNSUPPORTED_SWAP_FINDING",
                        "序列号差异不能被写成买方调包或更换硬件的既定事实。",
                        f"$.established_facts[{index}].statement",
                        rule_id="DM-EVIDENCE-01",
                    )

            for index, finding in enumerate(findings):
                refs = set(finding.get("evidence_ids", []))
                if not refs.issubset(evidence_ids):
                    violation(
                        "UNKNOWN_EVIDENCE_REFERENCE",
                        "主张认定引用了其他案件或不存在的证据。",
                        f"$.claim_findings[{index}].evidence_ids",
                    )
                citation_ids = finding.get("policy_citation_ids", [])
                if not citation_ids and (skill is None or skill.guard_profile().require_policy_citations):
                    violation(
                        "FINDING_WITHOUT_POLICY_CITATION",
                        "每项主张认定必须引用政策。",
                        f"$.claim_findings[{index}].policy_citation_ids",
                    )
                for citation_id in citation_ids:
                    citation = citation_index.get(citation_id)
                    if citation is None:
                        violation(
                            "UNKNOWN_POLICY_CITATION",
                            "主张认定引用了无法追溯的政策引用。",
                            f"$.claim_findings[{index}].policy_citation_ids",
                        )
                    elif (
                        citation.get("policy_id") != dispute.policy_id
                        or citation.get("policy_version") != dispute.policy_version
                    ):
                        violation(
                            "POLICY_CITATION_VERSION_MISMATCH",
                            "政策引用不属于案件固定版本。",
                            f"$.claim_findings[{index}].policy_citation_ids",
                        )

            transaction_amount = dispute.transaction.paid_amount_minor
            refund = payload.get("refund_amount")
            refund_amount = refund.get("amount_minor") if isinstance(refund, dict) else None
            checks["transaction_amount_minor"] = transaction_amount
            checks["refund_amount_minor"] = refund_amount
            if refund_amount is not None and (refund_amount < 0 or refund_amount > transaction_amount):
                violation(
                    "REFUND_OVERPAID_AMOUNT",
                    "退款金额必须在零与交易实付金额之间。",
                    "$.refund_amount.amount_minor",
                    rule_id="DM-REMEDY-01",
                )
            if decision.outcome == "RETURN_AND_FULL_REFUND":
                if refund_amount != transaction_amount:
                    violation("FULL_REFUND_AMOUNT_MISMATCH", "全额退款必须等于交易实付金额。", "$.refund_amount")
                expected_shipping_payer = "NOT_APPLICABLE" if skill and skill.manifest.name == "empty-package" else "SELLER"
                checks["expected_full_refund_shipping_payer"] = expected_shipping_payer
                if payload.get("shipping_payer") != expected_shipping_payer:
                    violation(
                        "SHIPPING_PAYER_MISMATCH",
                        f"当前 Skill 的全额退款运费字段应为 {expected_shipping_payer}。",
                        "$.shipping_payer",
                        rule_id=self._rule_id(skill, "REMEDY-01"),
                    )
            elif decision.outcome == "PARTIAL_REFUND":
                if refund_amount is None or not 0 < refund_amount < transaction_amount:
                    violation("PARTIAL_REFUND_AMOUNT_INVALID", "部分退款必须大于零且小于实付金额。", "$.refund_amount")
            elif refund_amount not in {None, 0}:
                violation("UNEXPECTED_REFUND_AMOUNT", "当前处置结果不应包含退款金额。", "$.refund_amount")

            actions = list(
                session.scalars(
                    select(ResolutionAction)
                    .where(ResolutionAction.decision_id == decision.id)
                    .order_by(ResolutionAction.created_at)
                )
            )
            prior_successful_actions = list(
                session.scalars(
                    select(ResolutionAction).where(
                        ResolutionAction.dispute_id == case_id,
                        ResolutionAction.decision_id != decision.id,
                        ResolutionAction.status == "SUCCEEDED",
                    )
                )
            )
            checks["prior_successful_action_types"] = sorted(
                {item.action_type for item in prior_successful_actions}
            )
            action_type_counts = Counter(item.action_type for item in actions)
            if any(count > 1 for count in action_type_counts.values()):
                violation("DUPLICATE_RESOLUTION_ACTION", "同一决定包含重复处置动作。", "$.resolution_actions")
            for action in actions:
                expected_key = f"{case_id}:{action.action_type}:{decision.version}"
                if action.idempotency_key != expected_key:
                    violation("INVALID_IDEMPOTENCY_KEY", "处置动作幂等键不符合固定格式。", "$.resolution_actions")
                if action.amount_minor is not None and action.amount_minor > transaction_amount:
                    violation("ACTION_AMOUNT_EXCEEDS_PAYMENT", "处置动作金额超过交易实付金额。", "$.resolution_actions")
                if action.status != "DRAFT":
                    violation("ACTION_NOT_DRAFT", "人工审核前处置动作必须保持 DRAFT。", "$.resolution_actions")
            refund_actions = [item for item in actions if item.action_type in {"FULL_REFUND", "PARTIAL_REFUND"}]
            prior_action_types = {item.action_type for item in prior_successful_actions}
            repeated_settlement = (
                bool(refund_actions)
                and (
                    bool(prior_action_types & {"FULL_REFUND", "PARTIAL_REFUND"})
                    or dispute.transaction.funds_status in {"REFUNDED", "PARTIALLY_REFUNDED"}
                )
            ) or (
                any(item.action_type == "RELEASE_FUNDS" for item in actions)
                and (
                    "RELEASE_FUNDS" in prior_action_types
                    or dispute.transaction.funds_status == "RELEASED"
                )
            ) or (
                any(item.action_type == "CREATE_RETURN" for item in actions)
                and (
                    "CREATE_RETURN" in prior_action_types
                    or dispute.transaction.order_status in {"RETURN_REQUESTED", "REFUND_COMPLETED"}
                )
            )
            checks["repeated_settlement_action"] = repeated_settlement
            if repeated_settlement:
                violation(
                    "REPEATED_SETTLEMENT_ACTION",
                    "当前决定会重复执行已经完成的退款、放款或退货动作，必须转人工处理。",
                    "$.resolution_actions",
                )
            if decision.outcome in {"RETURN_AND_FULL_REFUND", "PARTIAL_REFUND"}:
                if len(refund_actions) != 1 or refund_actions[0].amount_minor != refund_amount:
                    violation("REFUND_ACTION_MISMATCH", "退款动作与决定退款金额不一致。", "$.resolution_actions")
            elif refund_actions:
                violation("UNEXPECTED_REFUND_ACTION", "非退款结果不能创建退款动作。", "$.resolution_actions")

            case_evidence = list(session.scalars(select(Evidence).where(Evidence.dispute_id == case_id)))
            all_facts = [fact for item in case_evidence for fact in item.extracted_facts_json]
            listing_serials = {fact.get("value") for fact in all_facts if fact.get("field") == "listing_serial"}
            detected_serials = {fact.get("value") for fact in all_facts if fact.get("field") == "detected_serial"}
            serial_conflict = bool(listing_serials and detected_serials and listing_serials.isdisjoint(detected_serials))
            hardware_claim = any(item.claim_type == "HARDWARE_SWAP_ALLEGATION" for item in claims)
            checks["serial_or_hardware_swap_dispute"] = serial_conflict or hardware_claim
            if (serial_conflict or hardware_claim) and decision.outcome != "ESCALATE_TO_HUMAN":
                violation(
                    "SERIAL_CONFLICT_NOT_ESCALATED",
                    "序列号冲突或硬件调包指控必须转人工处理。",
                    "$.outcome",
                    rule_id="DM-EVIDENCE-01",
                )

            if skill is not None:
                matched_conditions = self._matched_skill_conditions(
                    skill.manifest.name,
                    skill.guard_profile().mandatory_escalation_conditions,
                    claims,
                    case_evidence,
                )
                checks["matched_skill_escalation_conditions"] = matched_conditions
                if matched_conditions and decision.outcome != "ESCALATE_TO_HUMAN":
                    violation(
                        "SKILL_MANDATORY_ESCALATION_NOT_ENFORCED",
                        "命中 Skill 强制人工条件但处置结果没有转人工："
                        + ", ".join(matched_conditions),
                        "$.outcome",
                        rule_id=self._rule_id(skill, "ESCALATE-01"),
                    )
                self._apply_skill_specific_checks(
                    skill.manifest.name,
                    payload,
                    decision.outcome,
                    refund_amount,
                    transaction_amount,
                    findings,
                    case_evidence,
                    actions,
                    checks,
                    violation,
                )

            profile_requires_review = skill is None or skill.guard_profile().human_review_required
            if profile_requires_review and (
                payload.get("requires_human_review") is not True or decision.requires_human_review is not True
            ):
                violation(
                    "HUMAN_REVIEW_NOT_ENFORCED",
                    "当前 Skill Guard Profile 要求所有决定明确进入人工审核。",
                    "$.requires_human_review",
                    rule_id=self._rule_id(skill, "ESCALATE-01"),
                )
            if policy is not None and refund_amount is not None:
                limit = policy.document_json["authority"]["auto_suggestion_refund_limit"]["amount_minor"]
                checks["auto_suggestion_refund_limit_minor"] = limit
                if refund_amount > limit:
                    violation(
                        "SUGGESTION_LIMIT_EXCEEDED",
                        "建议退款金额超过政策自动建议高风险阈值，必须保留人工审核。",
                        "$.refund_amount.amount_minor",
                        severity="WARN",
                        rule_id="DM-ESCALATE-01",
                    )

            passed = not any(item.severity == "BLOCK" for item in violations)
            required_action = "PROCEED_TO_HUMAN_REVIEW" if passed else "RETURN_TO_ADJUDICATION"
            checked_at = utc_now()
            result_payload = {
                "guard_result_id": new_id("guard"),
                "case_id": case_id,
                "case_run_id": run.id,
                "decision_id": decision.id,
                "guard_version": GUARD_VERSION,
                "passed": passed,
                "violations": [item.model_dump(mode="json") for item in violations],
                "checks": checks,
                "required_action": required_action,
                "decision_content_sha256": decision.content_sha256,
                "checked_at": checked_at,
            }
            result_hash = content_hash(result_payload)
            report = DecisionGuardReport(
                id=result_payload["guard_result_id"],
                dispute_id=case_id,
                case_run_id=run.id,
                decision_id=decision.id,
                guard_version=GUARD_VERSION,
                passed=passed,
                required_action=required_action,
                violations_json=result_payload["violations"],
                checks_json=jsonable(checks),
                decision_content_sha256=decision.content_sha256,
                content_sha256=result_hash,
                created_at=checked_at,
            )
            session.add(report)
            session.commit()
            return self._result(report)

    def get(self, guard_result_id: str) -> DecisionGuardResult:
        with self.session_factory() as session:
            report = session.get(DecisionGuardReport, guard_result_id)
            if report is None:
                raise NotFoundError(f"Guard 结果不存在: {guard_result_id}")
            return self._result(report)

    def list_for_case(self, case_id: str) -> list[DecisionGuardResult]:
        with self.session_factory() as session:
            reports = session.scalars(
                select(DecisionGuardReport)
                .where(DecisionGuardReport.dispute_id == case_id)
                .order_by(DecisionGuardReport.created_at)
            )
            return [self._result(item) for item in reports]

    @staticmethod
    def _rule_id(skill, suffix: str) -> str | None:  # type: ignore[no-untyped-def]
        if skill is None:
            return None
        prefix = {
            "description-mismatch": "DM",
            "missing-parts": "MP",
            "empty-package": "EP",
            "shipping-damage": "SD",
        }.get(skill.manifest.name)
        return f"{prefix}-{suffix}" if prefix else None

    @staticmethod
    def _truthy_fact(evidence: list[Evidence], field: str) -> bool:
        for item in evidence:
            for fact in item.extracted_facts_json:
                if fact.get("field") != field:
                    continue
                value = fact.get("value")
                if value is None or value is False or value == "" or value == 0 or value == [] or value == {}:
                    continue
                if str(value).casefold() not in {"false", "none", "null"}:
                    return True
        return False

    @staticmethod
    def _recorded_fact(evidence: list[Evidence], field: str) -> bool:
        return any(
            fact.get("field") == field and fact.get("value") is not None
            for item in evidence
            for fact in item.extracted_facts_json
        )

    @classmethod
    def _matched_skill_conditions(
        cls,
        skill_name: str,
        conditions: tuple[str, ...],
        claims: list[Claim],
        evidence: list[Evidence],
    ) -> list[str]:
        facts = [fact for item in evidence for fact in item.extracted_facts_json]
        handling_flags = {flag.upper() for item in evidence for flag in item.handling_flags_json}
        matched: list[str] = []
        for condition in conditions:
            inferred = condition in handling_flags or cls._truthy_fact(evidence, condition.casefold())
            if condition == "SERIAL_OR_HARDWARE_SWAP_DISPUTE":
                listing = {fact.get("value") for fact in facts if fact.get("field") == "listing_serial"}
                detected = {fact.get("value") for fact in facts if fact.get("field") == "detected_serial"}
                inferred = inferred or bool(listing and detected and listing.isdisjoint(detected)) or any(
                    item.claim_type == "HARDWARE_SWAP_ALLEGATION" for item in claims
                )
            elif condition == "HIGH_VALUE_COMPONENT_MISSING":
                inferred = inferred or cls._truthy_fact(evidence, "high_value_component_missing")
            elif condition == "PACKAGE_CONTENTS_MATERIALLY_CONTESTED":
                inferred = inferred or cls._truthy_fact(evidence, "package_contents_contested")
            elif condition == "WEIGHT_CHAIN_CONFLICT":
                inferred = inferred or cls._truthy_fact(evidence, "weight_chain_conflict")
            elif condition == "PACKING_OR_OPENING_RECORD_CONTESTED":
                inferred = inferred or cls._truthy_fact(evidence, "packing_or_opening_record_contested")
            elif condition == "FRAUD_ALLEGATION":
                inferred = inferred or any(
                    word in item.statement for item in claims for word in ("欺诈", "骗", "伪造")
                )
            elif condition == "DAMAGE_CAUSATION_CONTESTED":
                inferred = inferred or cls._truthy_fact(evidence, "damage_causation_contested")
            elif condition == "LATE_DAMAGE_REPORT":
                inferred = inferred or any(item.claim_type == "LATE_DAMAGE_REPORT" for item in claims) or cls._truthy_fact(
                    evidence, "late_damage_report"
                )
            elif condition == "CARRIER_LIABILITY_REQUIRED":
                inferred = inferred or cls._truthy_fact(evidence, "carrier_liability_required")
            elif condition == "EVIDENCE_AUTHENTICITY_CONTESTED":
                inferred = inferred or "EVIDENCE_AUTHENTICITY_CONTESTED" in handling_flags
            if inferred:
                matched.append(condition)
        return sorted(set(matched))

    @classmethod
    def _apply_skill_specific_checks(
        cls,
        skill_name: str,
        payload: dict[str, Any],
        outcome: str,
        refund_amount: int | None,
        transaction_amount: int,
        findings: list[dict[str, Any]],
        evidence: list[Evidence],
        actions: list[ResolutionAction],
        checks: dict[str, Any],
        violation,
    ) -> None:  # type: ignore[no-untyped-def]
        refund_outcome = outcome in {"RETURN_AND_FULL_REFUND", "PARTIAL_REFUND"}
        supported_refs = {
            evidence_id
            for finding in findings
            if finding.get("finding") in {"SUPPORTED", "PARTIALLY_SUPPORTED"}
            for evidence_id in finding.get("evidence_ids", [])
        }
        referenced_sources = {
            item.submitted_by for item in evidence if item.id in supported_refs
        }
        checks["supported_finding_sources"] = sorted(referenced_sources)

        if skill_name == "missing-parts" and refund_outcome:
            if referenced_sources and referenced_sources <= {"BUYER"}:
                violation(
                    "MISSING_PARTS_UNILATERAL_FINDING",
                    "不得仅凭买方单方材料认定缺件并生成退款建议。",
                    "$.claim_findings",
                    rule_id="MP-BURDEN-01",
                )
            if outcome == "PARTIAL_REFUND":
                supported_amounts = {
                    fact.get("value")
                    for item in evidence
                    for fact in item.extracted_facts_json
                    if fact.get("field") == "suggested_refund_amount_minor"
                }
                checks["skill_supported_refund_amounts"] = sorted(
                    item for item in supported_amounts if isinstance(item, int)
                )
                if refund_amount not in supported_amounts:
                    violation(
                        "MISSING_PARTS_REFUND_AMOUNT_UNSOURCED",
                        "缺件部分退款金额必须来自案件证据，Agent 不得自行估价。",
                        "$.refund_amount",
                        rule_id="MP-REMEDY-01",
                    )

        if skill_name == "empty-package" and refund_outcome:
            has_weight = any(
                cls._truthy_fact(evidence, field)
                for field in ("pickup_weight_grams", "transit_weight_grams", "delivery_weight_grams", "package_weight_grams")
            )
            has_packing = cls._truthy_fact(evidence, "packing_contents")
            has_opening = cls._recorded_fact(evidence, "opening_contents")
            has_waybill = cls._truthy_fact(evidence, "waybill_id")
            checks["empty_package_evidence_chain"] = {
                "weight": has_weight,
                "packing": has_packing,
                "opening": has_opening,
                "waybill": has_waybill,
            }
            if not all((has_weight, has_packing, has_opening, has_waybill)):
                violation(
                    "EMPTY_PACKAGE_CHAIN_INCOMPLETE",
                    "空包退款必须同时具备重量、打包、首次开包和运单关联材料。",
                    "$.claim_findings",
                    rule_id="EP-BURDEN-01",
                )
            if not referenced_sources or referenced_sources <= {"BUYER"}:
                violation(
                    "EMPTY_PACKAGE_BUYER_STATEMENT_ONLY",
                    "空包全额退款不得仅依据买方单方材料。",
                    "$.claim_findings",
                    rule_id="EP-EVIDENCE-01",
                )
            if any(item.action_type == "CREATE_RETURN" for item in actions):
                violation(
                    "EMPTY_PACKAGE_RETURN_ACTION_INVALID",
                    "空包全额退款不应创建无实物可退的退货动作。",
                    "$.resolution_actions",
                    rule_id="EP-REMEDY-01",
                )

        if skill_name == "shipping-damage" and refund_outcome:
            has_precondition = any(
                cls._truthy_fact(evidence, field) for field in ("preshipment_condition", "packaging_condition")
            )
            has_delivery = any(
                cls._truthy_fact(evidence, field) for field in ("delivery_condition", "damage_condition")
            ) and cls._truthy_fact(evidence, "damage_reported_at")
            checks["shipping_damage_condition_chain"] = {
                "preshipment_or_packaging": has_precondition,
                "delivery_and_report_time": has_delivery,
            }
            if not has_precondition or not has_delivery:
                violation(
                    "SHIPPING_DAMAGE_CHAIN_INCOMPLETE",
                    "运输损坏退款必须覆盖发货前/包装状态、签收损坏状态和报损时间。",
                    "$.claim_findings",
                    rule_id="SD-BURDEN-01",
                )
            if payload.get("shipping_payer") == "SELLER" and not cls._truthy_fact(
                evidence, "seller_packaging_inadequate"
            ):
                violation(
                    "CARRIER_LIABILITY_ASSIGNED_TO_SELLER",
                    "没有包装不足证据时，不能把可能的承运责任直接归给卖方。",
                    "$.shipping_payer",
                    rule_id="SD-EVIDENCE-01",
                )
            if refund_amount is not None and refund_amount > transaction_amount:
                violation(
                    "SHIPPING_DAMAGE_REFUND_EXCEEDS_PAYMENT",
                    "运输损坏退款金额不能超过交易实付金额。",
                    "$.refund_amount",
                    rule_id="SD-REMEDY-01",
                )

    @staticmethod
    def _result(report: DecisionGuardReport) -> DecisionGuardResult:
        return DecisionGuardResult(
            guard_result_id=report.id,
            case_id=report.dispute_id,
            case_run_id=report.case_run_id,
            decision_id=report.decision_id,
            guard_version=report.guard_version,
            passed=report.passed,
            violations=[GuardViolation.model_validate(item) for item in report.violations_json],
            checks=report.checks_json,
            required_action=report.required_action,  # type: ignore[arg-type]
            decision_content_sha256=report.decision_content_sha256,
            content_sha256=report.content_sha256,
            checked_at=report.created_at,
        )
