from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from dispute_agent.agent_schemas import (
    ClaimAssessment,
    ClaimFinding,
    DecisionRecommendation,
    EstablishedFact,
    EvidenceAssessment,
    EvidenceConflict,
    EvidencePolicyReport,
    PartyAnalysis,
    PolicyCitation,
    ProposedAction,
    QuestionProposal,
    TimelineEntry,
)
from dispute_agent.ids import new_id
from dispute_agent.models import utc_now


def _facts(evidence: list[dict[str, Any]], *fields: str) -> list[tuple[str, Any]]:
    wanted = set(fields)
    found: list[tuple[str, Any]] = []
    for item in evidence:
        for fact in item.get("extracted_facts", []):
            if fact.get("field") in wanted:
                found.append((item["evidence_id"], fact.get("value")))
    return found


def _first_fact(evidence: list[dict[str, Any]], *fields: str) -> tuple[str | None, Any | None]:
    values = _facts(evidence, *fields)
    return values[0] if values else (None, None)


def _related(evidence: list[dict[str, Any]], claim_id: str) -> list[dict[str, Any]]:
    return [item for item in evidence if claim_id in item.get("related_claim_ids", [])]


def _evidence_ids(items: list[dict[str, Any]], submitted_by: set[str] | None = None) -> list[str]:
    return [
        item["evidence_id"]
        for item in items
        if submitted_by is None or item.get("submitted_by") in submitted_by
    ]


def _question(
    *,
    target: str,
    question: str,
    missing_fact: str,
    claim_id: str,
    evidence_types: list[str],
    basis_ids: list[str],
    generation_reason: str = "MATERIAL_EVIDENCE_GAP",
) -> QuestionProposal:
    return QuestionProposal(
        target=target,  # type: ignore[arg-type]
        question=question,
        missing_fact=missing_fact,
        resolves_claim_ids=[claim_id],
        acceptable_evidence_types=evidence_types,
        basis_evidence_ids=sorted(set(basis_ids)),
        generation_reason=generation_reason,
    )


def _build_description_mismatch_party_analysis(input_data: dict[str, Any]) -> PartyAnalysis:
    party = input_data["party"]
    claims = [item for item in input_data["claims"] if item["claim_id"] in input_data["analyzed_claim_ids"]]
    evidence = input_data["evidence"]
    listing = input_data["listing"]["payload"]
    promised_memory = listing.get("memory_gb")
    listing_serial = listing.get("device_serial")
    detected_evidence_id, detected_memory = _first_fact(evidence, "detected_memory_gb")
    detected_serial_evidence_id, detected_serial = _first_fact(evidence, "detected_serial")
    seller_report_id, preshipment_memory = _first_fact(evidence, "preshipment_memory_gb")
    seller_serial_id, preshipment_serial = _first_fact(evidence, "preshipment_serial")
    listing_evidence_ids = [item["evidence_id"] for item in evidence if item["evidence_type"] == "LISTING_SNAPSHOT"]

    assessments: list[ClaimAssessment] = []
    questions: list[QuestionProposal] = []
    response_points: list[str] = []
    for claim in claims:
        claim_id = claim["claim_id"]
        relevant = _related(evidence, claim_id)
        buyer_ids = _evidence_ids(relevant, {"BUYER", "SYSTEM", "THIRD_PARTY"})
        seller_ids = _evidence_ids(relevant, {"SELLER", "SYSTEM"})
        gaps: list[str] = []

        if claim["claim_type"] == "CONFIG_MISMATCH" and claim["party"] == "BUYER":
            if detected_memory is None:
                gaps.append("缺少收到设备实际内存容量的可核验证据。")
                questions.append(
                    _question(
                        target="BUYER",
                        question="请提交能够显示检测时间、设备序列号和内存容量的系统信息或第三方检测原报告。",
                        missing_fact="收到设备的实际内存容量及检测对象与涉案设备的关联",
                        claim_id=claim_id,
                        evidence_types=["DEVICE_REPORT", "DOCUMENT", "CHAT_SNAPSHOT"],
                        basis_ids=buyer_ids,
                    )
                )
            elif detected_serial is None:
                gaps.append("现有配置材料没有显示设备或主板序列号。")
                questions.append(
                    _question(
                        target="BUYER",
                        question="请补充能够在同一画面或同一报告中显示设备序列号与内存容量的材料。",
                        missing_fact="检测材料与涉案设备的身份关联",
                        claim_id=claim_id,
                        evidence_types=["DEVICE_REPORT", "DOCUMENT", "CHAT_SNAPSHOT"],
                        basis_ids=buyer_ids,
                    )
                )

            if party == "BUYER":
                if detected_memory is None:
                    position = "UNDETERMINED"
                    reasoning = "卖方承诺可以确认，但买方尚未证明收到设备的实际配置。"
                elif promised_memory != detected_memory:
                    position = "PARTIAL" if detected_serial and listing_serial and detected_serial != listing_serial else "SUPPORT"
                    reasoning = (
                        f"交易前描述为 {promised_memory}GB，检测材料显示 {detected_memory}GB；"
                        + ("但序列号冲突使交付设备身份仍需核验。" if position == "PARTIAL" else "且设备身份未见冲突。")
                    )
                else:
                    position = "OPPOSE"
                    reasoning = "当前检测材料显示的内存与交易前描述一致。"
                supporting = sorted(set(listing_evidence_ids + ([detected_evidence_id] if detected_evidence_id else [])))
                contrary = seller_ids if seller_report_id else []
            else:
                if seller_report_id and preshipment_memory == promised_memory:
                    position = "OPPOSE"
                    reasoning = "卖方存在发货前配置记录，但仍需与收货后材料和设备身份连续性共同核验。"
                elif detected_memory is not None and promised_memory != detected_memory:
                    position = "PARTIAL" if detected_serial != listing_serial else "UNDETERMINED"
                    reasoning = "买方材料显示配置差异；卖方缺少足以直接排除描述不符的发货前连续记录。"
                else:
                    position = "UNDETERMINED"
                    reasoning = "当前材料不足以证明或排除买方主张。"
                supporting = seller_ids
                contrary = buyer_ids

        elif claim["claim_type"] == "HARDWARE_SWAP_ALLEGATION":
            if seller_report_id is None or preshipment_serial is None:
                gaps.append("缺少发货前同时显示设备身份和配置的材料。")
                questions.append(
                    _question(
                        target="SELLER",
                        question="请提交发货前形成、能够同时显示设备或主板序列号与内存配置的原始记录。",
                        missing_fact="发货前涉案设备的身份和配置",
                        claim_id=claim_id,
                        evidence_types=["DEVICE_REPORT", "DOCUMENT", "CHAT_SNAPSHOT"],
                        basis_ids=seller_ids,
                    )
                )
            if party == "SELLER":
                if seller_report_id and detected_serial and preshipment_serial != detected_serial:
                    position = "PARTIAL"
                    reasoning = "发货前与检测时序列号存在差异，但该差异不能单独证明买方实施调包。"
                else:
                    position = "UNDETERMINED"
                    reasoning = "推测、物流重量或单方陈述不能单独证明买方更换硬件。"
                supporting = seller_ids
                contrary = buyer_ids
            else:
                position = "OPPOSE" if seller_report_id is None else "UNDETERMINED"
                reasoning = "卖方当前缺少发货前设备身份与配置的直接材料，不能把调包指控写成事实。"
                supporting = buyer_ids
                contrary = seller_ids
            response_points.append("序列号差异最多证明记录冲突，不能直接证明差异由哪一方造成。")

        elif claim["claim_type"] == "SERIAL_MISMATCH":
            mismatch = bool(listing_serial and detected_serial and listing_serial != detected_serial)
            position = "SUPPORT" if mismatch else "UNDETERMINED"
            reasoning = "两个可追溯来源中的序列号不同。" if mismatch else "当前材料尚不能确认序列号差异。"
            supporting = sorted(set(listing_evidence_ids + ([detected_serial_evidence_id] if detected_serial_evidence_id else [])))
            contrary = []
        else:
            position = "UNDETERMINED"
            reasoning = "当前基线仅实现笔记本配置、序列号和调包相关主张。"
            supporting = []
            contrary = []

        assessments.append(
            ClaimAssessment(
                claim_id=claim_id,
                position=position,  # type: ignore[arg-type]
                supporting_evidence_ids=sorted(set(supporting)),
                contrary_evidence_ids=sorted(set(contrary)),
                evidence_gaps=gaps,
                reasoning_summary=reasoning,
            )
        )

    unique_questions: dict[tuple[str, str, tuple[str, ...]], QuestionProposal] = {}
    for item in questions:
        key = (item.target, item.missing_fact.casefold(), tuple(sorted(item.resolves_claim_ids)))
        unique_questions[key] = item
    return PartyAnalysis(
        analysis_id=new_id("analysis"),
        case_id=input_data["case_id"],
        case_run_id=input_data["case_run_id"],
        party=party,
        analyzed_claim_ids=input_data["analyzed_claim_ids"],
        claim_assessments=assessments,
        response_points=sorted(set(response_points)),
        proposed_questions=list(unique_questions.values()),
        summary=(
            f"{party} 视角分析了 {len(assessments)} 项关键主张，"
            f"识别 {sum(len(item.evidence_gaps) for item in assessments)} 个证据缺口；"
            "所有不确定结论均保留为未确定或部分支持。"
        ),
        generated_at=utc_now(),
    )


def _assess_evidence(item: dict[str, Any]) -> EvidenceAssessment:
    evidence_type = item["evidence_type"]
    integrity = item["integrity_status"]
    findings = [
        f"{fact.get('field')}={fact.get('value')}"
        for fact in item.get("extracted_facts", [])
    ]
    limitations: list[str] = []
    if evidence_type == "LISTING_SNAPSHOT":
        relevance, reliability, temporal, probative = "HIGH", "HIGH", "DIRECT", "DIRECT"
    elif evidence_type == "DEVICE_REPORT":
        quality = next(
            (fact.get("value") for fact in item.get("extracted_facts", []) if fact.get("field") == "quality"),
            None,
        )
        reliability = "HIGH" if quality and "THIRD_PARTY" in str(quality) else "MEDIUM"
        relevance, temporal, probative = "HIGH", "NEAR_EVENT", "CORROBORATIVE"
        limitations.append("检测报告直接证明检测时状态，通常不能单独证明签收瞬间状态。")
    elif evidence_type == "DOCUMENT":
        quality = next(
            (fact.get("value") for fact in item.get("extracted_facts", []) if fact.get("field") == "quality"),
            None,
        )
        reliability = "HIGH" if quality in {"PLATFORM_TEXT_EXPORT", "THIRD_PARTY_TEXT_REPORT"} else "MEDIUM"
        relevance, temporal, probative = "HIGH", "NEAR_EVENT", "CORROBORATIVE"
        limitations.append("文字记录可证明记录形成时的设备信息，需结合来源哈希、时间和设备标识。")
    elif evidence_type == "CHAT_SNAPSHOT":
        relevance, reliability, temporal, probative = "HIGH", "HIGH", "DIRECT", "DIRECT"
        limitations.append("聊天快照直接证明当事人表述，不当然证明表述内容真实。")
    elif evidence_type == "VIDEO":
        relevance, reliability, temporal, probative = "HIGH", "MEDIUM", "NEAR_EVENT", "CORROBORATIVE"
        limitations.append("需确认视频连续性、形成时间和设备身份。")
    elif evidence_type == "PHOTO":
        relevance, reliability, temporal, probative = "MEDIUM", "LOW", "UNKNOWN", "CONTEXT_ONLY"
        limitations.append("单张图片可能缺少形成时间、上下文或设备身份。")
    elif evidence_type == "PARTY_STATEMENT":
        relevance, reliability, temporal, probative = "MEDIUM", "LOW", "REMOTE", "CONTEXT_ONLY"
        limitations.append("单方陈述不能单独证明争议事实。")
    else:
        relevance, reliability, temporal, probative = "MEDIUM", "MEDIUM", "UNKNOWN", "CONTEXT_ONLY"
    if item.get("handling_flags"):
        limitations.append(f"证据处理标记：{', '.join(item['handling_flags'])}。")
    authenticity = "VERIFIED" if integrity in {"SOURCE_VERIFIED", "HASH_VERIFIED"} else "UNVERIFIED"
    usability = "USABLE" if authenticity == "VERIFIED" and reliability in {"HIGH", "MEDIUM"} else "USABLE_WITH_LIMITS"
    return EvidenceAssessment(
        assessment_id=new_id("assessment"),
        evidence_id=item["evidence_id"],
        usability=usability,  # type: ignore[arg-type]
        relevance=relevance,  # type: ignore[arg-type]
        authenticity=authenticity,  # type: ignore[arg-type]
        reliability=reliability,  # type: ignore[arg-type]
        temporal_fit=temporal,  # type: ignore[arg-type]
        probative_value=probative,  # type: ignore[arg-type]
        findings=findings or [item["description"]],
        limitations=limitations,
        related_claim_ids=item.get("related_claim_ids", []),
    )


def _build_description_mismatch_evidence_policy_report(input_data: dict[str, Any]) -> EvidencePolicyReport:
    evidence = input_data["evidence"]
    claims = [item for item in input_data["claims"] if item["claim_id"] in input_data["analyzed_claim_ids"]]
    policy = input_data["policy"]
    transaction = input_data["transaction"]
    listing = input_data["listing"]
    listing_ids = [item["evidence_id"] for item in evidence if item["evidence_type"] == "LISTING_SNAPSHOT"]
    timeline: list[TimelineEntry] = [
        TimelineEntry(
            timeline_event_id=new_id("timeline"),
            event_type="LISTING_BASELINE_CAPTURED",
            occurred_at=datetime.fromisoformat(listing["captured_at"]),
            description="争议创建时锁定商品发布页基线。",
            source_evidence_ids=listing_ids,
            confidence=1.0,
            disputed=False,
        ),
        TimelineEntry(
            timeline_event_id=new_id("timeline"),
            event_type="TRANSACTION_PAID",
            occurred_at=datetime.fromisoformat(transaction["paid_at"]),
            description="买方完成交易支付。",
            source_evidence_ids=[],
            confidence=1.0,
            disputed=False,
        ),
    ]
    if transaction.get("delivered_at"):
        timeline.append(
            TimelineEntry(
                timeline_event_id=new_id("timeline"),
                event_type="SHIPMENT_DELIVERED",
                occurred_at=datetime.fromisoformat(transaction["delivered_at"]),
                description="物流记录显示交易已签收。",
                source_evidence_ids=[],
                confidence=1.0,
                disputed=False,
            )
        )
    for item in evidence:
        timeline.append(
            TimelineEntry(
                timeline_event_id=new_id("timeline"),
                event_type="EVIDENCE_CAPTURED",
                occurred_at=datetime.fromisoformat(item["source"]["captured_at"]),
                description=item["description"],
                source_evidence_ids=[item["evidence_id"]],
                confidence=1.0 if item["integrity_status"] in {"SOURCE_VERIFIED", "HASH_VERIFIED"} else 0.5,
                disputed=item["submitted_by"] in {"BUYER", "SELLER"},
            )
        )
    timeline.sort(key=lambda item: item.occurred_at)

    policy_id = policy["policy_id"]
    policy_version = policy["version"]
    claim_ids = [item["claim_id"] for item in claims]
    seller_claim_ids = [item["claim_id"] for item in claims if item["party"] == "SELLER"]
    serial_claim_ids = [
        item["claim_id"]
        for item in claims
        if item["claim_type"] in {"SERIAL_MISMATCH", "HARDWARE_SWAP_ALLEGATION"}
    ]
    citations: list[PolicyCitation] = []
    selected_rule_ids = {
        "DM-DEF-01",
        "DM-BURDEN-01",
        "DM-BURDEN-02",
        "DM-EVIDENCE-01",
        "DM-EVIDENCE-02",
        "DM-REMEDY-01",
        "DM-INSUFFICIENT-01",
        "DM-ESCALATE-01",
    }
    for rule in policy["rules"]:
        if rule["rule_id"] not in selected_rule_ids:
            continue
        applies = claim_ids
        if rule["rule_id"] == "DM-BURDEN-02":
            applies = seller_claim_ids or claim_ids
        elif rule["rule_id"] == "DM-EVIDENCE-01":
            applies = serial_claim_ids
        if not applies:
            continue
        citations.append(
            PolicyCitation(
                citation_id=new_id("citation"),
                policy_id=policy_id,
                policy_version=policy_version,
                rule_id=rule["rule_id"],
                citation_key=rule["citation_key"],
                applies_to_claim_ids=applies,
                rationale=f"{rule['title']} 与当前主张、证据要求或处置边界相关。",
            )
        )

    promised_id, promised_memory = _first_fact(evidence, "promised_memory_gb")
    detected_id, detected_memory = _first_fact(evidence, "detected_memory_gb")
    listing_serial_id, listing_serial = _first_fact(evidence, "listing_serial")
    detected_serial_id, detected_serial = _first_fact(evidence, "detected_serial")
    seller_memory_id, seller_memory = _first_fact(evidence, "preshipment_memory_gb")
    seller_serial_id, seller_serial = _first_fact(evidence, "preshipment_serial")
    conflicts: list[EvidenceConflict] = []
    if promised_memory is not None and detected_memory is not None and promised_memory != detected_memory:
        conflicts.append(
            EvidenceConflict(
                conflict_id=new_id("conflict"),
                description=f"交易前描述为 {promised_memory}GB，检测材料显示 {detected_memory}GB。",
                evidence_ids=sorted({promised_id, detected_id} - {None}),  # type: ignore[arg-type]
                material=True,
                resolution_status="OPEN",
                resolution="该差异支持继续审查描述不符，但仍需结合设备身份和时间连续性。",
            )
        )
    if listing_serial and detected_serial and listing_serial != detected_serial:
        conflicts.append(
            EvidenceConflict(
                conflict_id=new_id("conflict"),
                description=f"发布页序列号 {listing_serial} 与检测材料序列号 {detected_serial} 不同。",
                evidence_ids=sorted({listing_serial_id, detected_serial_id} - {None}),  # type: ignore[arg-type]
                material=True,
                resolution_status="REQUIRES_HUMAN",
                resolution=None,
            )
        )
    if seller_memory is not None and detected_memory is not None and seller_memory != detected_memory:
        conflicts.append(
            EvidenceConflict(
                conflict_id=new_id("conflict"),
                description=f"卖方发货前材料显示 {seller_memory}GB，买方检测材料显示 {detected_memory}GB。",
                evidence_ids=sorted({seller_memory_id, detected_id} - {None}),  # type: ignore[arg-type]
                material=True,
                resolution_status="REQUIRES_HUMAN",
                resolution=None,
            )
        )

    questions: list[QuestionProposal] = []
    buyer_config_claims = [item for item in claims if item["party"] == "BUYER" and item["claim_type"] == "CONFIG_MISMATCH"]
    for claim in buyer_config_claims:
        if detected_memory is None:
            questions.append(
                _question(
                    target="BUYER",
                    question="请提交能够显示检测时间、设备序列号和内存容量的系统信息或第三方检测原报告。",
                    missing_fact="收到设备的实际配置和设备身份",
                    claim_id=claim["claim_id"],
                    evidence_types=["DEVICE_REPORT", "DOCUMENT", "CHAT_SNAPSHOT"],
                    basis_ids=_evidence_ids(_related(evidence, claim["claim_id"])),
                )
            )
        elif detected_serial is None:
            questions.append(
                _question(
                    target="BUYER",
                    question="请补充在同一材料中显示设备序列号与内存容量的证据。",
                    missing_fact="检测对象与涉案设备的身份关联",
                    claim_id=claim["claim_id"],
                    evidence_types=["DEVICE_REPORT", "DOCUMENT", "CHAT_SNAPSHOT"],
                    basis_ids=[detected_id] if detected_id else [],
                )
            )
    hardware_claims = [item for item in claims if item["claim_type"] == "HARDWARE_SWAP_ALLEGATION"]
    for claim in hardware_claims:
        if seller_memory is None or seller_serial is None:
            questions.append(
                _question(
                    target="SELLER",
                    question="请提交发货前形成、能够同时显示设备或主板序列号与内存配置的原始记录。",
                    missing_fact="发货前涉案设备的身份和配置",
                    claim_id=claim["claim_id"],
                    evidence_types=["DEVICE_REPORT", "DOCUMENT", "CHAT_SNAPSHOT"],
                    basis_ids=_evidence_ids(_related(evidence, claim["claim_id"])),
                )
            )

    human_reasons = [
        item.description for item in conflicts if item.resolution_status == "REQUIRES_HUMAN"
    ]
    if hardware_claims:
        human_reasons.append("案件包含调包或更换硬件指控，政策要求人工审核。")
    unresolved = [item.description for item in conflicts if item.resolution_status != "RESOLVED"]
    unresolved.extend(item.missing_fact for item in questions)
    return EvidencePolicyReport(
        report_id=new_id("report"),
        case_id=input_data["case_id"],
        case_run_id=input_data["case_run_id"],
        analyzed_claim_ids=input_data["analyzed_claim_ids"],
        policy_id=policy_id,
        policy_version=policy_version,
        policy_basis_time=datetime.fromisoformat(input_data["case_state"]["policy_selection"]["basis_time"]),
        timeline=timeline,
        evidence_assessments=[_assess_evidence(item) for item in evidence],
        policy_citations=citations,
        conflicts=conflicts,
        proposed_questions=questions,
        unresolved_issues=sorted(set(unresolved)),
        requires_human_review=bool(human_reasons),
        human_review_reasons=sorted(set(human_reasons)),
        generated_at=utc_now(),
    )


def _build_description_mismatch_decision_recommendation(input_data: dict[str, Any]) -> DecisionRecommendation:
    claims = input_data["claims"]
    analyzed_ids = set(input_data.get("analyzed_claim_ids", []))
    previous_decision = input_data.get("previous_decision") or {}
    previous_findings = {
        item["claim_id"]: item
        for item in previous_decision.get("claim_findings", [])
        if isinstance(item, dict) and item.get("claim_id")
    }
    if not previous_findings:
        analyzed_ids = {item["claim_id"] for item in claims}
    evidence = input_data["evidence"]
    report = input_data["evidence_report"]
    transaction = input_data["transaction"]
    citations = report["policy_citations"]
    citation_by_rule: dict[str, list[str]] = defaultdict(list)
    for citation in citations:
        citation_by_rule[citation["rule_id"]].append(citation["citation_id"])
    fallback_citation = citations[0]["citation_id"] if citations else "citation_missing"

    promised_id, promised_memory = _first_fact(evidence, "promised_memory_gb")
    detected_id, detected_memory = _first_fact(evidence, "detected_memory_gb")
    listing_serial_id, listing_serial = _first_fact(evidence, "listing_serial")
    detected_serial_id, detected_serial = _first_fact(evidence, "detected_serial")
    seller_memory_id, seller_memory = _first_fact(evidence, "preshipment_memory_gb")
    seller_serial_id, seller_serial = _first_fact(evidence, "preshipment_serial")
    serial_conflict = bool(listing_serial and detected_serial and listing_serial != detected_serial)
    seller_buyer_conflict = bool(
        seller_memory is not None and detected_memory is not None and seller_memory != detected_memory
    )

    findings: list[ClaimFinding] = []
    established: list[EstablishedFact] = []
    if promised_id and promised_memory is not None:
        established.append(
            EstablishedFact(
                fact_id=new_id("fact"),
                statement=f"交易前商品材料明确描述内存为 {promised_memory}GB。",
                evidence_ids=[promised_id],
            )
        )
    if detected_id and detected_memory is not None:
        established.append(
            EstablishedFact(
                fact_id=new_id("fact"),
                statement=f"检测材料记载检测时设备内存为 {detected_memory}GB。",
                evidence_ids=[detected_id],
            )
        )
    if serial_conflict and listing_serial_id and detected_serial_id:
        established.append(
            EstablishedFact(
                fact_id=new_id("fact"),
                statement=f"发布页序列号 {listing_serial} 与检测材料序列号 {detected_serial} 不同。",
                evidence_ids=[listing_serial_id, detected_serial_id],
            )
        )
    if seller_memory_id and seller_memory is not None:
        established.append(
            EstablishedFact(
                fact_id=new_id("fact"),
                statement=f"卖方发货前材料记载内存为 {seller_memory}GB。",
                evidence_ids=[seller_memory_id],
            )
        )

    supported_buyer_config = False
    has_insufficient = False
    has_hardware_claim = any(item["claim_type"] == "HARDWARE_SWAP_ALLEGATION" for item in claims)
    for claim in claims:
        claim_id = claim["claim_id"]
        if claim_id not in analyzed_ids:
            preserved = previous_findings.get(claim_id)
            if preserved is not None:
                findings.append(ClaimFinding.model_validate(preserved))
            continue
        if claim["claim_type"] == "CONFIG_MISMATCH" and claim["party"] == "BUYER":
            evidence_ids = sorted({promised_id, detected_id} - {None})  # type: ignore[arg-type]
            citation_ids = citation_by_rule.get("DM-DEF-01", []) + citation_by_rule.get("DM-BURDEN-01", [])
            if detected_memory is None:
                finding = "INSUFFICIENT_EVIDENCE"
                rationale = "缺少收到设备实际配置的可核验证据。"
                has_insufficient = True
                citation_ids += citation_by_rule.get("DM-INSUFFICIENT-01", [])
            elif promised_memory != detected_memory:
                finding = "PARTIALLY_SUPPORTED" if serial_conflict or seller_buyer_conflict else "SUPPORTED"
                rationale = (
                    f"交易前描述为 {promised_memory}GB，检测材料显示 {detected_memory}GB。"
                    + ("设备身份或发货前后材料仍存在重大冲突。" if finding == "PARTIALLY_SUPPORTED" else "现有设备身份未见冲突。")
                )
                supported_buyer_config = True
                citation_ids += citation_by_rule.get("DM-REMEDY-01", [])
            else:
                finding = "NOT_SUPPORTED"
                rationale = "检测材料显示的内存容量与交易前描述一致。"
            findings.append(
                ClaimFinding(
                    claim_id=claim_id,
                    finding=finding,  # type: ignore[arg-type]
                    rationale=rationale,
                    evidence_ids=evidence_ids,
                    policy_citation_ids=sorted(set(citation_ids or [fallback_citation])),
                )
            )
        elif claim["claim_type"] == "HARDWARE_SWAP_ALLEGATION":
            ids = sorted({seller_memory_id, seller_serial_id, detected_serial_id} - {None})  # type: ignore[arg-type]
            findings.append(
                ClaimFinding(
                    claim_id=claim_id,
                    finding="INSUFFICIENT_EVIDENCE",
                    rationale="序列号或配置差异不能单独证明买方更换硬件，当前缺少直接、连续证据。",
                    evidence_ids=ids,
                    policy_citation_ids=sorted(
                        set(
                            citation_by_rule.get("DM-BURDEN-02", [])
                            + citation_by_rule.get("DM-EVIDENCE-01", [])
                            or [fallback_citation]
                        )
                    ),
                )
            )

        elif claim["claim_type"] == "SERIAL_MISMATCH":
            findings.append(
                ClaimFinding(
                    claim_id=claim_id,
                    finding="SUPPORTED" if serial_conflict else "INSUFFICIENT_EVIDENCE",
                    rationale="两个可追溯来源中的序列号不同，但该事实不证明差异原因。" if serial_conflict else "当前材料不能确认序列号冲突。",
                    evidence_ids=sorted({listing_serial_id, detected_serial_id} - {None}),  # type: ignore[arg-type]
                    policy_citation_ids=sorted(set(citation_by_rule.get("DM-EVIDENCE-01", []) or [fallback_citation])),
                )
            )
        else:
            has_insufficient = True
            findings.append(
                ClaimFinding(
                    claim_id=claim_id,
                    finding="INSUFFICIENT_EVIDENCE",
                    rationale="当前 MVP 调查方法尚不能充分认定该主张。",
                    evidence_ids=[],
                    policy_citation_ids=sorted(set(citation_by_rule.get("DM-INSUFFICIENT-01", []) or [fallback_citation])),
                )
            )

    preserved_facts = [
        EstablishedFact.model_validate(item)
        for item in previous_decision.get("established_facts", [])
        if isinstance(item, dict) and item.get("evidence_ids")
    ]
    fact_index = {
        (item.statement, tuple(sorted(item.evidence_ids))): item
        for item in [*preserved_facts, *established]
    }
    established = list(fact_index.values())
    claim_by_id = {item["claim_id"]: item for item in claims}
    supported_buyer_config = any(
        claim_by_id.get(item.claim_id, {}).get("claim_type") == "CONFIG_MISMATCH"
        and claim_by_id.get(item.claim_id, {}).get("party") == "BUYER"
        and item.finding in {"SUPPORTED", "PARTIALLY_SUPPORTED"}
        for item in findings
    )
    has_insufficient = any(item.finding == "INSUFFICIENT_EVIDENCE" for item in findings)

    human_reasons = ["MVP 中所有处置建议必须由人工审核后才能执行。"]
    human_reasons.extend(report.get("human_review_reasons", []))
    preserved_claim_ids = sorted(set(previous_findings) - analyzed_ids)
    if preserved_claim_ids:
        human_reasons.append(
            f"本轮仅重评受新证据影响的主张；其余 {len(preserved_claim_ids)} 项沿用上一决定版本并保留引用。"
        )
    open_question_ids = [item["question_id"] for item in input_data.get("open_questions", []) if item["status"] == "OPEN"]
    if open_question_ids:
        outcome = "REQUEST_MORE_EVIDENCE"
        refund = None
        shipping = "UNDETERMINED"
        actions: list[ProposedAction] = []
        uncertainty = "HIGH"
    elif report.get("requires_human_review") or serial_conflict or seller_buyer_conflict or has_hardware_claim:
        outcome = "ESCALATE_TO_HUMAN"
        refund = None
        shipping = "UNDETERMINED"
        actions = []
        uncertainty = "HIGH"
    elif supported_buyer_config:
        outcome = "RETURN_AND_FULL_REFUND"
        refund = transaction["paid_amount"]["amount_minor"]
        shipping = "SELLER"
        actions = [
            ProposedAction(action_type="CREATE_RETURN", amount_minor=None),
            ProposedAction(action_type="FULL_REFUND", amount_minor=refund),
        ]
        uncertainty = "LOW"
    elif has_insufficient:
        outcome = "REJECT_CLAIM"
        refund = None
        shipping = "NOT_APPLICABLE"
        actions = []
        uncertainty = "MEDIUM"
    else:
        outcome = "REJECT_CLAIM"
        refund = None
        shipping = "NOT_APPLICABLE"
        actions = []
        uncertainty = "LOW"

    all_citation_ids = sorted({citation_id for finding in findings for citation_id in finding.policy_citation_ids})
    return DecisionRecommendation(
        recommendation_id=new_id("recommendation"),
        case_id=input_data["case_id"],
        case_run_id=input_data["case_run_id"],
        outcome=outcome,  # type: ignore[arg-type]
        claim_findings=findings,
        established_facts=established,
        refund_amount_minor=refund,
        shipping_payer=shipping,  # type: ignore[arg-type]
        unresolved_question_ids=open_question_ids,
        proposed_actions=actions,
        policy_citation_ids=all_citation_ids or [fallback_citation],
        requires_human_review=True,
        human_review_reasons=sorted(set(human_reasons)),
        uncertainty=uncertainty,  # type: ignore[arg-type]
        reviewer_explanation=(
            "建议人工核验双方独立分析、证据来源、时间线和政策引用。"
            + ("案件存在设备身份、调包或发货前后材料冲突，不能自动推定责任。" if outcome == "ESCALATE_TO_HUMAN" else "")
        ),
        user_explanation=(
            "系统已根据交易快照、双方材料和交易时有效规则生成审核建议。"
            + ("现有材料存在重大冲突，将交由人工进一步核验。" if outcome == "ESCALATE_TO_HUMAN" else "最终结果需经人工确认。")
        ),
        generated_at=utc_now(),
    )


REQUIREMENT_FACT_GROUPS: dict[str, tuple[tuple[str, ...], ...]] = {
    "MP-PROMISED-CONTENTS": (("promised_items", "promised_quantity"),),
    "MP-RECEIVED-CONTENTS": (("received_items", "received_quantity", "missing_items"),),
    "EP-WEIGHT-CHAIN": (("pickup_weight_grams", "transit_weight_grams", "delivery_weight_grams", "package_weight_grams"),),
    "EP-PACKING-OR-OPENING": (("packing_contents",), ("opening_contents",)),
    "SD-PRE-SHIPMENT-CONDITION": (("preshipment_condition", "packaging_condition"),),
    "SD-DELIVERY-CONDITION": (("delivery_condition", "damage_condition"), ("damage_reported_at",)),
}

QUESTION_REQUIRED_FACTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "MP-Q-CONTENTS": (("received_items", "received_quantity", "missing_items"),),
    "MP-Q-PACKING": (("preshipment_items", "packing_contents"),),
    "EP-Q-WEIGHT": (("pickup_weight_grams", "transit_weight_grams", "delivery_weight_grams", "package_weight_grams"),),
    "EP-Q-PACKING": (("packing_contents",),),
    "EP-Q-OPENING": (("opening_contents",),),
    "SD-Q-PRECONDITION": (("preshipment_condition", "packaging_condition"),),
    "SD-Q-DELIVERY": (("delivery_condition", "damage_condition"), ("damage_reported_at",)),
}

FACT_LABELS = {
    "promised_items": "交易约定包含的物品",
    "promised_quantity": "交易约定数量",
    "received_items": "签收时实际收到的物品",
    "received_quantity": "签收时实际收到数量",
    "missing_items": "主张缺失的物品",
    "preshipment_items": "发货前装箱物品",
    "packing_contents": "卖方打包内容",
    "opening_contents": "买方首次开包内容",
    "pickup_weight_grams": "揽收重量（克）",
    "transit_weight_grams": "运输节点重量（克）",
    "delivery_weight_grams": "签收节点重量（克）",
    "package_weight_grams": "包裹重量（克）",
    "waybill_id": "运单号",
    "preshipment_condition": "发货前商品状态",
    "packaging_condition": "包装状态",
    "delivery_condition": "签收时商品状态",
    "damage_condition": "损坏状态",
    "damage_reported_at": "首次报损时间",
    "carrier_exception": "物流异常",
    "suggested_refund_amount_minor": "有材料支持的建议退款金额（分）",
}


def _skill_name(input_data: dict[str, Any]) -> str:
    manifests = input_data.get("bound_skills", [])
    return str(manifests[0].get("name")) if len(manifests) == 1 else "description-mismatch"


def _manifest(input_data: dict[str, Any]) -> dict[str, Any]:
    manifests = input_data.get("bound_skills", [])
    if len(manifests) != 1:
        raise ValueError("规则基线要求输入中存在唯一绑定 Skill")
    return manifests[0]


def _fact_index(evidence: list[dict[str, Any]]) -> dict[str, list[tuple[str, Any]]]:
    index: dict[str, list[tuple[str, Any]]] = defaultdict(list)
    for item in evidence:
        for fact in item.get("extracted_facts", []):
            field = fact.get("field")
            if field:
                index[str(field)].append((item["evidence_id"], fact.get("value")))
    return index


def _present(value: Any) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return False
    if isinstance(value, str) and value.strip().casefold() in {"false", "no", "none", "null", "0"}:
        return False
    return value is not False


def _groups_present(facts: dict[str, list[tuple[str, Any]]], groups: tuple[tuple[str, ...], ...]) -> bool:
    return all(
        any(
            (field in {"opening_contents", "received_items", "missing_items"} and value is not None)
            or _present(value)
            for field in group
            for _evidence_id, value in facts.get(field, [])
        )
        for group in groups
    )


def _requirement_satisfied(requirement: dict[str, Any], evidence: list[dict[str, Any]], claim_id: str) -> bool:
    relevant = _related(evidence, claim_id)
    facts = _fact_index(relevant)
    groups = REQUIREMENT_FACT_GROUPS.get(requirement["requirement_id"])
    if groups is not None:
        return _groups_present(facts, groups)
    accepted = set(requirement.get("accepted_evidence_types", []))
    return any(item.get("evidence_type") in accepted for item in relevant)


def _claim_requirements(manifest: dict[str, Any], claim_type: str) -> list[dict[str, Any]]:
    return [
        item
        for item in manifest.get("evidence_requirements", [])
        if claim_type in item.get("applies_to_claim_types", [])
        and item.get("mandatory_for_supported_finding", True)
    ]


def _resolve_question_target(target: str, claimant: str) -> str:
    if target in {"BUYER", "SELLER"}:
        return target
    if target == "RESPONDENT":
        return "SELLER" if claimant == "BUYER" else "BUYER"
    return claimant


def _skill_questions(input_data: dict[str, Any]) -> list[QuestionProposal]:
    manifest = _manifest(input_data)
    evidence = input_data.get("evidence", [])
    questions: dict[tuple[str, str, tuple[str, ...]], QuestionProposal] = {}
    analyzed_ids = set(input_data.get("analyzed_claim_ids", []))
    for claim in input_data.get("claims", []):
        if analyzed_ids and claim["claim_id"] not in analyzed_ids:
            continue
        relevant = _related(evidence, claim["claim_id"])
        facts = _fact_index(relevant)
        for rule in manifest.get("question_rules", []):
            if claim["claim_type"] not in rule.get("applies_to_claim_types", []):
                continue
            groups = QUESTION_REQUIRED_FACTS.get(rule["rule_id"])
            if groups is None or _groups_present(facts, groups):
                continue
            target = _resolve_question_target(rule["target"], claim["party"])
            proposal = _question(
                target=target,
                question=rule["question_template"],
                missing_fact=rule["trigger"],
                claim_id=claim["claim_id"],
                evidence_types=list(rule["acceptable_evidence_types"]),
                basis_ids=_evidence_ids(relevant),
                generation_reason=f"SKILL_RULE:{rule['rule_id']}",
            )
            key = (proposal.target, proposal.missing_fact, tuple(proposal.resolves_claim_ids))
            questions[key] = proposal
    return list(questions.values())


def _matched_escalation_conditions(input_data: dict[str, Any]) -> list[str]:
    manifest = _manifest(input_data)
    evidence = input_data.get("evidence", [])
    facts = _fact_index(evidence)
    claims = input_data.get("claims", [])
    flags = {
        str(flag).upper()
        for item in evidence
        for flag in item.get("handling_flags", [])
    }
    matched: list[str] = []
    for condition in manifest["guard_profile"].get("mandatory_escalation_conditions", []):
        fact_field = condition.casefold()
        explicit = any(_present(value) for _evidence_id, value in facts.get(fact_field, []))
        inferred = False
        if condition == "SERIAL_OR_HARDWARE_SWAP_DISPUTE":
            listing = {value for _id, value in facts.get("listing_serial", []) if _present(value)}
            detected = {value for _id, value in facts.get("detected_serial", []) if _present(value)}
            inferred = bool(listing and detected and listing.isdisjoint(detected)) or any(
                item.get("claim_type") == "HARDWARE_SWAP_ALLEGATION" for item in claims
            )
        elif condition == "HIGH_VALUE_COMPONENT_MISSING":
            inferred = any(item.get("claim_type") == "MISSING_COMPONENT" for item in claims) and any(
                _present(value) for _id, value in facts.get("high_value_component_missing", [])
            )
        elif condition == "PACKAGE_CONTENTS_MATERIALLY_CONTESTED":
            inferred = any(_present(value) for _id, value in facts.get("package_contents_contested", []))
        elif condition == "WEIGHT_CHAIN_CONFLICT":
            inferred = any(_present(value) for _id, value in facts.get("weight_chain_conflict", []))
        elif condition == "PACKING_OR_OPENING_RECORD_CONTESTED":
            inferred = any(
                _present(value) for _id, value in facts.get("packing_or_opening_record_contested", [])
            )
        elif condition == "FRAUD_ALLEGATION":
            inferred = any(
                word in str(item.get("statement", "")) for item in claims for word in ("欺诈", "骗", "伪造")
            )
        elif condition == "DAMAGE_CAUSATION_CONTESTED":
            inferred = any(_present(value) for _id, value in facts.get("damage_causation_contested", []))
        elif condition == "LATE_DAMAGE_REPORT":
            inferred = any(item.get("claim_type") == "LATE_DAMAGE_REPORT" for item in claims) or any(
                _present(value) for _id, value in facts.get("late_damage_report", [])
            )
        elif condition == "CARRIER_LIABILITY_REQUIRED":
            inferred = any(_present(value) for _id, value in facts.get("carrier_liability_required", []))
        elif condition == "EVIDENCE_AUTHENTICITY_CONTESTED":
            inferred = "EVIDENCE_AUTHENTICITY_CONTESTED" in flags
        if explicit or inferred or condition in flags:
            matched.append(condition)
    return sorted(set(matched))


def _build_skill_party_analysis(input_data: dict[str, Any]) -> PartyAnalysis:
    party = input_data["party"]
    manifest = _manifest(input_data)
    evidence = input_data.get("evidence", [])
    analyzed_ids = set(input_data.get("analyzed_claim_ids", []))
    matched_escalations = _matched_escalation_conditions(input_data)
    assessments: list[ClaimAssessment] = []
    for claim in input_data.get("claims", []):
        if analyzed_ids and claim["claim_id"] not in analyzed_ids:
            continue
        relevant = _related(evidence, claim["claim_id"])
        requirements = _claim_requirements(manifest, claim["claim_type"])
        missing = [item for item in requirements if not _requirement_satisfied(item, evidence, claim["claim_id"])]
        supporting = _evidence_ids(relevant, {claim["party"], "SYSTEM", "THIRD_PARTY"})
        contrary_party = "SELLER" if claim["party"] == "BUYER" else "BUYER"
        contrary = _evidence_ids(relevant, {contrary_party})
        if missing:
            position = "UNDETERMINED"
            reasoning = f"{manifest['title']}仍缺少 {len(missing)} 项强制证据要求。"
        elif matched_escalations:
            position = "PARTIAL"
            reasoning = "关键证据已出现，但命中强制人工条件，不能由规则基线直接完成责任归属。"
        elif party == claim["party"]:
            position = "SUPPORT"
            reasoning = "当前材料覆盖该 Skill 对本主张的强制证据要求。"
        elif contrary:
            position = "OPPOSE"
            reasoning = "本方提交了与该主张相反的材料，仍需证据 Clerk 统一核验。"
        else:
            position = "UNDETERMINED"
            reasoning = "本方没有足以直接反驳该主张的独立材料。"
        assessments.append(
            ClaimAssessment(
                claim_id=claim["claim_id"],
                position=position,  # type: ignore[arg-type]
                supporting_evidence_ids=sorted(set(supporting)),
                contrary_evidence_ids=sorted(set(contrary)),
                evidence_gaps=[item["description"] for item in missing],
                reasoning_summary=reasoning,
            )
        )
    questions = [item for item in _skill_questions(input_data) if item.target == party]
    return PartyAnalysis(
        analysis_id=new_id("analysis"),
        case_id=input_data["case_id"],
        case_run_id=input_data["case_run_id"],
        party=party,
        analyzed_claim_ids=input_data["analyzed_claim_ids"],
        claim_assessments=assessments,
        response_points=[
            f"本轮使用 {manifest['name']}@{manifest['version']}，只读取其声明的上下文并执行其证据要求。"
        ],
        proposed_questions=questions,
        summary=f"{party} 视角按 {manifest['title']}分析 {len(assessments)} 项主张，提出 {len(questions)} 个定向补问。",
        generated_at=utc_now(),
    )


def _build_skill_evidence_policy_report(input_data: dict[str, Any]) -> EvidencePolicyReport:
    manifest = _manifest(input_data)
    evidence = input_data.get("evidence", [])
    claims = [
        item for item in input_data.get("claims", [])
        if item["claim_id"] in set(input_data.get("analyzed_claim_ids", []))
    ]
    policy = input_data["policy"]
    transaction = input_data["transaction"]
    timeline: list[TimelineEntry] = []
    listing = input_data.get("listing")
    if listing is not None:
        listing_ids = [item["evidence_id"] for item in evidence if item["evidence_type"] == "LISTING_SNAPSHOT"]
        timeline.append(
            TimelineEntry(
                timeline_event_id=new_id("timeline"),
                event_type="LISTING_BASELINE_CAPTURED",
                occurred_at=datetime.fromisoformat(listing["captured_at"]),
                description="争议创建时锁定商品发布页基线。",
                source_evidence_ids=listing_ids,
                confidence=1.0,
                disputed=False,
            )
        )
    timeline.append(
        TimelineEntry(
            timeline_event_id=new_id("timeline"),
            event_type="TRANSACTION_PAID",
            occurred_at=datetime.fromisoformat(transaction["paid_at"]),
            description="买方完成交易支付。",
            source_evidence_ids=[],
            confidence=1.0,
            disputed=False,
        )
    )
    for event in input_data.get("shipment", []):
        timeline.append(
            TimelineEntry(
                timeline_event_id=new_id("timeline"),
                event_type=event["event_type"],
                occurred_at=datetime.fromisoformat(event["occurred_at"]),
                description=f"物流节点：{event['event_type']}。",
                source_evidence_ids=[],
                confidence=1.0,
                disputed=False,
            )
        )
    for item in evidence:
        timeline.append(
            TimelineEntry(
                timeline_event_id=new_id("timeline"),
                event_type="EVIDENCE_CAPTURED",
                occurred_at=datetime.fromisoformat(item["source"]["captured_at"]),
                description=item["description"],
                source_evidence_ids=[item["evidence_id"]],
                confidence=1.0 if item["integrity_status"] in {"SOURCE_VERIFIED", "HASH_VERIFIED"} else 0.5,
                disputed=item["submitted_by"] in {"BUYER", "SELLER"},
            )
        )
    timeline.sort(key=lambda item: item.occurred_at)

    claim_ids = [item["claim_id"] for item in claims]
    citations = [
        PolicyCitation(
            citation_id=new_id("citation"),
            policy_id=policy["policy_id"],
            policy_version=policy["version"],
            rule_id=rule["rule_id"],
            citation_key=rule["citation_key"],
            applies_to_claim_ids=claim_ids,
            rationale=f"{rule['title']} 属于 {manifest['title']} 的调查或处置边界。",
        )
        for rule in policy["rules"]
        if claim_ids
    ]
    questions = _skill_questions(input_data)
    missing_requirements: list[str] = []
    for claim in claims:
        for requirement in _claim_requirements(manifest, claim["claim_type"]):
            if not _requirement_satisfied(requirement, evidence, claim["claim_id"]):
                missing_requirements.append(requirement["description"])

    matched = _matched_escalation_conditions(input_data)
    conflicts: list[EvidenceConflict] = []
    evidence_ids = _evidence_ids(evidence)
    for condition in matched:
        if ("CONFLICT" in condition or "CONTESTED" in condition) and len(evidence_ids) >= 2:
            conflicts.append(
                EvidenceConflict(
                    conflict_id=new_id("conflict"),
                    description=f"命中 {condition}，关键材料需要人工比较来源、时间和关联性。",
                    evidence_ids=evidence_ids[:2],
                    material=True,
                    resolution_status="REQUIRES_HUMAN",
                    resolution=None,
                )
            )
    human_reasons = [f"Skill Guard Profile 命中强制人工条件：{item}。" for item in matched]
    unresolved = missing_requirements + [item.missing_fact for item in questions]
    return EvidencePolicyReport(
        report_id=new_id("report"),
        case_id=input_data["case_id"],
        case_run_id=input_data["case_run_id"],
        analyzed_claim_ids=input_data["analyzed_claim_ids"],
        policy_id=policy["policy_id"],
        policy_version=policy["version"],
        policy_basis_time=datetime.fromisoformat(input_data["case_state"]["policy_selection"]["basis_time"]),
        timeline=timeline,
        evidence_assessments=[_assess_evidence(item) for item in evidence],
        policy_citations=citations,
        conflicts=conflicts,
        proposed_questions=questions,
        unresolved_issues=sorted(set(unresolved)),
        requires_human_review=bool(matched),
        human_review_reasons=human_reasons,
        generated_at=utc_now(),
    )


def _citation_ids(report: dict[str, Any], prefix: str, *suffixes: str) -> list[str]:
    wanted = {f"{prefix}-{suffix}" for suffix in suffixes}
    selected = [
        item["citation_id"]
        for item in report.get("policy_citations", [])
        if item.get("rule_id") in wanted
    ]
    if selected:
        return sorted(set(selected))
    citations = report.get("policy_citations", [])
    return [citations[0]["citation_id"]] if citations else ["citation_missing"]


def _build_skill_decision_recommendation(input_data: dict[str, Any]) -> DecisionRecommendation:
    manifest = _manifest(input_data)
    skill_name = manifest["name"]
    prefix = {"missing-parts": "MP", "empty-package": "EP", "shipping-damage": "SD"}[skill_name]
    claims = input_data["claims"]
    analyzed_ids = set(input_data.get("analyzed_claim_ids", []))
    evidence = input_data.get("evidence", [])
    report = input_data["evidence_report"]
    transaction_amount = input_data["transaction"]["paid_amount"]["amount_minor"]
    previous = input_data.get("previous_decision") or {}
    previous_findings = {
        item["claim_id"]: item for item in previous.get("claim_findings", [])
        if isinstance(item, dict) and item.get("claim_id")
    }
    facts = _fact_index(evidence)
    matched = _matched_escalation_conditions(input_data)
    findings: list[ClaimFinding] = []
    any_supported = False
    any_insufficient = False
    for claim in claims:
        if analyzed_ids and claim["claim_id"] not in analyzed_ids:
            preserved = previous_findings.get(claim["claim_id"])
            if preserved:
                findings.append(ClaimFinding.model_validate(preserved))
            continue
        requirements = _claim_requirements(manifest, claim["claim_type"])
        missing = [item for item in requirements if not _requirement_satisfied(item, evidence, claim["claim_id"])]
        refs = _evidence_ids(_related(evidence, claim["claim_id"]))
        if missing:
            finding = "INSUFFICIENT_EVIDENCE"
            rationale = f"仍缺少 {len(missing)} 项 {manifest['title']} 强制证据要求。"
            citations = _citation_ids(report, prefix, "BURDEN-01", "INSUFFICIENT-01")
            any_insufficient = True
        elif matched:
            finding = "PARTIALLY_SUPPORTED"
            rationale = "关键证据要求已覆盖，但命中 Skill 强制人工条件，责任归属仍需审核。"
            citations = _citation_ids(report, prefix, "EVIDENCE-01", "ESCALATE-01")
        else:
            finding = "SUPPORTED"
            rationale = f"现有材料覆盖 {manifest['title']} 对该主张的强制证据要求。"
            citations = _citation_ids(report, prefix, "DEF-01", "REMEDY-01")
            any_supported = True
        findings.append(
            ClaimFinding(
                claim_id=claim["claim_id"],
                finding=finding,  # type: ignore[arg-type]
                rationale=rationale,
                evidence_ids=refs,
                policy_citation_ids=citations,
            )
        )

    established: list[EstablishedFact] = []
    for field, values in facts.items():
        if field not in FACT_LABELS:
            continue
        evidence_id, value = values[0]
        if _present(value):
            established.append(
                EstablishedFact(
                    fact_id=new_id("fact"),
                    statement=f"{FACT_LABELS[field]}：{value}。",
                    evidence_ids=[evidence_id],
                )
            )
    open_question_ids = [
        item["question_id"] for item in input_data.get("open_questions", []) if item.get("status") == "OPEN"
    ]
    refund: int | None = None
    shipping = "NOT_APPLICABLE"
    actions: list[ProposedAction] = []
    uncertainty = "MEDIUM"
    if open_question_ids:
        outcome = "REQUEST_MORE_EVIDENCE"
        shipping = "UNDETERMINED"
        uncertainty = "HIGH"
    elif matched or report.get("requires_human_review"):
        outcome = "ESCALATE_TO_HUMAN"
        shipping = "UNDETERMINED"
        uncertainty = "HIGH"
    elif any_supported and skill_name == "missing-parts":
        _amount_id, suggested_amount = _first_fact(evidence, "suggested_refund_amount_minor")
        if isinstance(suggested_amount, int) and 0 < suggested_amount < transaction_amount:
            outcome = "PARTIAL_REFUND"
            refund = suggested_amount
            shipping = "NOT_APPLICABLE"
            actions = [ProposedAction(action_type="PARTIAL_REFUND", amount_minor=refund)]
            uncertainty = "LOW"
        elif any(_present(value) for _id, value in facts.get("full_refund_recommended", [])):
            outcome = "RETURN_AND_FULL_REFUND"
            refund = transaction_amount
            shipping = "SELLER"
            actions = [
                ProposedAction(action_type="CREATE_RETURN", amount_minor=None),
                ProposedAction(action_type="FULL_REFUND", amount_minor=refund),
            ]
            uncertainty = "MEDIUM"
        else:
            outcome = "ESCALATE_TO_HUMAN"
            shipping = "UNDETERMINED"
            uncertainty = "HIGH"
    elif any_supported and skill_name == "empty-package":
        outcome = "RETURN_AND_FULL_REFUND"
        refund = transaction_amount
        shipping = "NOT_APPLICABLE"
        actions = [ProposedAction(action_type="FULL_REFUND", amount_minor=refund)]
        uncertainty = "MEDIUM"
    elif any_supported and skill_name == "shipping-damage":
        _amount_id, suggested_amount = _first_fact(evidence, "suggested_refund_amount_minor")
        seller_packaging = any(_present(value) for _id, value in facts.get("seller_packaging_inadequate", []))
        if seller_packaging:
            outcome = "RETURN_AND_FULL_REFUND"
            refund = transaction_amount
            shipping = "SELLER"
            actions = [
                ProposedAction(action_type="CREATE_RETURN", amount_minor=None),
                ProposedAction(action_type="FULL_REFUND", amount_minor=refund),
            ]
            uncertainty = "MEDIUM"
        elif isinstance(suggested_amount, int) and 0 < suggested_amount < transaction_amount:
            outcome = "PARTIAL_REFUND"
            refund = suggested_amount
            actions = [ProposedAction(action_type="PARTIAL_REFUND", amount_minor=refund)]
        else:
            outcome = "ESCALATE_TO_HUMAN"
            shipping = "UNDETERMINED"
            uncertainty = "HIGH"
    elif any_insufficient:
        outcome = "REJECT_CLAIM"
    else:
        outcome = "REJECT_CLAIM"
        uncertainty = "LOW"

    citation_ids = sorted({item for finding in findings for item in finding.policy_citation_ids})
    human_reasons = ["MVP 中所有处置建议必须由人工审核后才能执行。"]
    human_reasons.extend(f"Skill Guard Profile 命中：{item}。" for item in matched)
    return DecisionRecommendation(
        recommendation_id=new_id("recommendation"),
        case_id=input_data["case_id"],
        case_run_id=input_data["case_run_id"],
        outcome=outcome,  # type: ignore[arg-type]
        claim_findings=findings,
        established_facts=established,
        refund_amount_minor=refund,
        shipping_payer=shipping,  # type: ignore[arg-type]
        unresolved_question_ids=open_question_ids,
        proposed_actions=actions,
        policy_citation_ids=citation_ids or ["citation_missing"],
        requires_human_review=True,
        human_review_reasons=sorted(set(human_reasons)),
        uncertainty=uncertainty,  # type: ignore[arg-type]
        reviewer_explanation=f"请按 {manifest['name']}@{manifest['version']} 的证据要求、政策引用和 Guard Profile 复核。",
        user_explanation="系统已按本争议类型专属调查方法生成建议；最终结果需经人工确认。",
        generated_at=utc_now(),
    )


def build_party_analysis(input_data: dict[str, Any]) -> PartyAnalysis:
    if _skill_name(input_data) == "description-mismatch":
        return _build_description_mismatch_party_analysis(input_data)
    return _build_skill_party_analysis(input_data)


def build_evidence_policy_report(input_data: dict[str, Any]) -> EvidencePolicyReport:
    if _skill_name(input_data) == "description-mismatch":
        return _build_description_mismatch_evidence_policy_report(input_data)
    return _build_skill_evidence_policy_report(input_data)


def build_decision_recommendation(input_data: dict[str, Any]) -> DecisionRecommendation:
    if _skill_name(input_data) == "description-mismatch":
        return _build_description_mismatch_decision_recommendation(input_data)
    return _build_skill_decision_recommendation(input_data)
