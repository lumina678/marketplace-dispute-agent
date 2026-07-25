from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dispute_agent.config import get_settings
from dispute_agent.db import SessionLocal
from dispute_agent.models import (
    Appeal,
    Approval,
    CaseEvent,
    Claim,
    Decision,
    Dispute,
    Evidence,
    ListingSnapshot,
    Message,
    PolicyVersion,
    ShipmentEvent,
    Transaction,
    User,
    utc_now,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def digest(value: str | bytes | dict[str, Any]) -> str:
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, str):
        value = value.encode("utf-8")
    return sha256(value).hexdigest()


SCENARIOS: list[dict[str, Any]] = [
    {
        "slug": "clear_mismatch",
        "title": "明确描述不符，支持买方",
        "paid_amount_minor": 320000,
        "promised_memory_gb": 16,
        "listing_serial": "SN-CLEAR-001",
        "buyer_report": {"memory_gb": 8, "serial": "SN-CLEAR-001", "quality": "PLATFORM_TEXT_EXPORT"},
        "seller_claim": None,
    },
    {
        "slug": "buyer_missing_evidence",
        "title": "买方证据不足，需要补充举证",
        "paid_amount_minor": 260000,
        "promised_memory_gb": 16,
        "listing_serial": "SN-MISSING-001",
        "buyer_report": None,
        "seller_claim": None,
    },
    {
        "slug": "seller_preshipment",
        "title": "卖方存在发货前检测记录",
        "paid_amount_minor": 280000,
        "promised_memory_gb": 16,
        "listing_serial": "SN-SELLER-001",
        "buyer_report": {"memory_gb": 8, "serial": "SN-SELLER-001", "quality": "BUYER_TEXT_EXPORT"},
        "seller_report": {"memory_gb": 16, "serial": "SN-SELLER-001", "quality": "PRESHIPMENT_TEXT_LOG"},
        "seller_claim": "卖方主张发货前检测为 16GB。",
    },
    {
        "slug": "serial_conflict",
        "title": "序列号冲突，必须人工处理",
        "paid_amount_minor": 420000,
        "promised_memory_gb": 16,
        "listing_serial": "SN-ORIGINAL-001",
        "buyer_report": {"memory_gb": 8, "serial": "SN-RECEIVED-999", "quality": "THIRD_PARTY_TEXT_REPORT"},
        "seller_claim": "卖方怀疑买方更换了主板或设备。",
    },
    {
        "slug": "appeal_reversal",
        "title": "新证据导致申诉后可能翻转",
        "paid_amount_minor": 350000,
        "promised_memory_gb": 16,
        "listing_serial": "SN-APPEAL-001",
        "buyer_report": {"memory_gb": 8, "serial": "SN-APPEAL-001", "quality": "NEW_THIRD_PARTY_TEXT_REPORT"},
        "seller_claim": "原审核因买方只有模糊截图而驳回，买方现提交第三方原报告。",
        "appealed": True,
    },
]


def _load_policy_versions(session: Session, policy_directory: Path) -> None:
    index = json.loads((policy_directory / "index.json").read_text(encoding="utf-8"))
    for entry in index["policies"]:
        document_path = policy_directory / entry["path"]
        document = json.loads(document_path.read_text(encoding="utf-8"))
        existing = session.scalar(
            select(PolicyVersion).where(
                PolicyVersion.policy_id == entry["policy_id"],
                PolicyVersion.version == entry["version"],
            )
        )
        if existing:
            continue
        session.add(
            PolicyVersion(
                id=f"policy_{entry['version'].replace('.', '_')}",
                policy_id=entry["policy_id"],
                version=entry["version"],
                status=entry["status"],
                effective_from=dt(entry["effective_from"]),
                effective_to=dt(entry["effective_to"]) if entry["effective_to"] else None,
                document_path=str(document_path.relative_to(policy_directory.parent)),
                document_json=document,
            )
        )


def _ensure_users(session: Session) -> None:
    users = [
        User(id="user_buyer_demo", role="BUYER", display_name="演示买家", simulated_balance_minor=1_000_000),
        User(id="user_seller_demo", role="SELLER", display_name="演示卖家", simulated_balance_minor=1_000_000),
        User(id="user_reviewer_demo", role="REVIEWER", display_name="演示审核员", simulated_balance_minor=0),
        User(id="user_admin_demo", role="ADMIN", display_name="演示管理员", simulated_balance_minor=0),
    ]
    for user in users:
        if session.get(User, user.id) is None:
            session.add(user)


def _add_evidence(
    session: Session,
    *,
    evidence_id: str,
    dispute_id: str,
    submitted_by: str,
    evidence_type: str,
    description: str,
    source_system: str,
    submitted_at: datetime,
    related_claim_ids: list[str],
    extracted_facts: list[dict[str, Any]],
) -> None:
    content_hash = digest(f"{evidence_id}:{description}")
    session.add(
        Evidence(
            id=evidence_id,
            dispute_id=dispute_id,
            submitted_by=submitted_by,
            evidence_type=evidence_type,
            description=description,
            source_system=source_system,
            source_record_id=evidence_id,
            captured_at=submitted_at,
            submitted_at=submitted_at,
            content_sha256=content_hash,
            immutable_uri=f"evidence://{dispute_id}/{evidence_id}",
            integrity_status="HASH_VERIFIED" if source_system == "EVIDENCE_STORE" else "SOURCE_VERIFIED",
            related_claim_ids_json=related_claim_ids,
            extracted_facts_json=extracted_facts,
            handling_flags_json=[],
        )
    )


def _seed_appeal_history(session: Session, dispute: Dispute, slug: str) -> None:
    decision_payload = {
        "outcome": "REJECT_CLAIM",
        "reason": "原始图片无法识别设备身份，证据不足。",
        "requires_human_review": True,
    }
    decision_hash = digest(decision_payload)
    decision = Decision(
        id=f"decision_{slug}_v1",
        dispute_id=dispute.id,
        case_run_id=None,
        version=1,
        supersedes_decision_id=None,
        outcome="REJECT_CLAIM",
        status="APPROVED",
        payload_json=decision_payload,
        content_sha256=decision_hash,
        requires_human_review=True,
    )
    session.add(decision)
    session.flush()
    session.add(
        Approval(
            id=f"approval_{slug}_v1",
            dispute_id=dispute.id,
            decision_id=decision.id,
            reviewer_id="user_reviewer_demo",
            action="APPROVE",
            reason="原始证据不足，批准驳回草稿。",
            decision_content_sha256=decision_hash,
        )
    )
    session.add(
        Appeal(
            id=f"appeal_{slug}_001",
            dispute_id=dispute.id,
            decision_id=decision.id,
            appellant_id="user_buyer_demo",
            appellant_role="BUYER",
            grounds="NEW_EVIDENCE",
            statement="提交能够显示设备序列号和内存容量的第三方原报告。",
            evidence_ids_json=[f"ev_{slug}_buyer_report"],
            policy_id="marketplace.description_mismatch",
            policy_version="2.0.0",
            decision_content_sha256=decision_hash,
            deadline=dt("2026-07-21T12:00:00+08:00"),
            status="SUBMITTED",
            submitted_at=dt("2026-07-16T12:00:00+08:00"),
        )
    )

    transitions = [
        ("T01", "BASELINE_CAPTURED", "SUBMITTED", "EVIDENCE_LOCKED"),
        ("T03", "INVESTIGATION_STARTED", "EVIDENCE_LOCKED", "UNDER_INVESTIGATION"),
        ("T13", "INVESTIGATION_COMPLETED", "UNDER_INVESTIGATION", "READY_FOR_REVIEW"),
        ("T15", "REVIEW_PACKAGE_SUBMITTED", "READY_FOR_REVIEW", "HUMAN_REVIEW"),
        ("T16", "DECISION_APPROVED", "HUMAN_REVIEW", "APPROVED"),
        ("T22", "NO_EXECUTION_REQUIRED", "APPROVED", "RESOLVED"),
        ("T27", "VALID_APPEAL_RECEIVED", "RESOLVED", "APPEALED"),
    ]
    base_time = dt("2026-07-15T10:00:00+08:00")
    for sequence, (transition_id, event_type, from_state, to_state) in enumerate(transitions, start=1):
        session.add(
            CaseEvent(
                id=f"event_{slug}_{sequence:02d}",
                dispute_id=dispute.id,
                sequence=sequence,
                event_type=event_type,
                transition_id=transition_id,
                from_state=from_state,
                to_state=to_state,
                actor_type="SYSTEM" if sequence != 5 else "REVIEWER",
                actor_id="seed",
                reason_code="SEEDED_APPEAL_HISTORY",
                expected_state_version=sequence,
                new_state_version=sequence + 1,
                metadata_json={"seeded": True},
                occurred_at=base_time + timedelta(minutes=sequence),
            )
        )
    dispute.state = "APPEALED"
    dispute.state_version = 8


def _seed_scenario(session: Session, scenario: dict[str, Any], index: int) -> None:
    slug = scenario["slug"]
    dispute_id = f"case_{slug}"
    if session.get(Dispute, dispute_id) is not None:
        return

    paid_at = dt(f"2026-07-{8 + index:02d}T09:30:00+08:00")
    delivered_at = paid_at + timedelta(days=3, hours=6)
    transaction_id = f"txn_{slug}"
    listing_id = f"listing_{slug}"
    listing_payload = {
        "title": f"二手笔记本演示案件：{scenario['title']}",
        "category": "USED_LAPTOP",
        "memory_gb": scenario["promised_memory_gb"],
        "condition": "功能正常",
        "repair_history": "无维修",
        "device_serial": scenario["listing_serial"],
    }
    transaction = Transaction(
        id=transaction_id,
        buyer_id="user_buyer_demo",
        seller_id="user_seller_demo",
        listing_id=listing_id,
        category="USED_LAPTOP",
        paid_amount_minor=scenario["paid_amount_minor"],
        currency="CNY",
        paid_at=paid_at,
        delivered_at=delivered_at,
        order_status="DISPUTED",
        funds_status="HELD",
    )
    session.add(transaction)
    session.add(
        ListingSnapshot(
            id=f"snapshot_{slug}",
            transaction_id=transaction_id,
            listing_id=listing_id,
            snapshot_type="DISPUTE_BASELINE",
            payload_json=listing_payload,
            content_sha256=digest(listing_payload),
            immutable_uri=f"snapshot://listing/{listing_id}",
            captured_at=delivered_at + timedelta(hours=18),
        )
    )
    dispute = Dispute(
        id=dispute_id,
        transaction_id=transaction_id,
        dispute_type="DESCRIPTION_MISMATCH",
        state="SUBMITTED",
        state_version=1,
        question_round_count=0,
        requires_human_review=True,
        human_review_reasons_json=[],
        created_at=delivered_at + timedelta(hours=17),
        updated_at=delivered_at + timedelta(hours=17),
    )
    session.add(dispute)
    session.flush()

    buyer_claim_id = f"claim_{slug}_buyer"
    session.add(
        Claim(
            id=buyer_claim_id,
            dispute_id=dispute_id,
            party="BUYER",
            claim_type="CONFIG_MISMATCH",
            issue_type="DESCRIPTION_MISMATCH",
            issue_subtype="CONFIG_MISMATCH",
            routing_source="USER_DECLARED",
            routing_reason="演示案件在提交时明确选择商品描述不符。",
            routing_confidence=1.0,
            skill_name="description-mismatch",
            skill_version="1.0.0",
            routing_status="ROUTED",
            statement=f"卖方承诺 {scenario['promised_memory_gb']}GB，买方主张实物配置不符。",
            status="ALLEGED",
            material=True,
            asserted_at=delivered_at + timedelta(hours=17),
        )
    )
    seller_claim_id: str | None = None
    if scenario.get("seller_claim"):
        seller_claim_id = f"claim_{slug}_seller"
        claim_type = "HARDWARE_SWAP_ALLEGATION" if slug == "serial_conflict" else "CONFIG_MISMATCH"
        session.add(
            Claim(
                id=seller_claim_id,
                dispute_id=dispute_id,
                party="SELLER",
                claim_type=claim_type,
                issue_type="DESCRIPTION_MISMATCH",
                issue_subtype=claim_type,
                routing_source="USER_DECLARED",
                routing_reason="卖方抗辩继承当前案件已声明的描述不符争议类型。",
                routing_confidence=1.0,
                skill_name="description-mismatch",
                skill_version="1.0.0",
                routing_status="ROUTED",
                statement=scenario["seller_claim"],
                status="ALLEGED",
                material=True,
                responds_to_claim_id=buyer_claim_id,
                asserted_at=delivered_at + timedelta(hours=19),
            )
        )

    chat_body = f"确认，这台机器是 {scenario['promised_memory_gb']}GB 内存，功能正常。"
    session.add(
        Message(
            id=f"message_{slug}_promise",
            transaction_id=transaction_id,
            dispute_id=dispute_id,
            sender_id="user_seller_demo",
            sender_role="SELLER",
            body=chat_body,
            sent_at=paid_at - timedelta(hours=2),
            content_sha256=digest(chat_body),
            snapshot_locked=True,
        )
    )
    session.add(
        ShipmentEvent(
            id=f"shipment_{slug}_delivered",
            transaction_id=transaction_id,
            event_type="DELIVERED",
            occurred_at=delivered_at,
            location="演示签收点",
            details_json={"weight_kg": 2.4, "signed_by": "BUYER"},
            source_sha256=digest(f"{transaction_id}:DELIVERED:{delivered_at.isoformat()}"),
        )
    )

    _add_evidence(
        session,
        evidence_id=f"ev_{slug}_listing",
        dispute_id=dispute_id,
        submitted_by="SYSTEM",
        evidence_type="LISTING_SNAPSHOT",
        description=f"商品快照标注 {scenario['promised_memory_gb']}GB，序列号 {scenario['listing_serial']}。",
        source_system="LISTING",
        submitted_at=dispute.created_at,
        related_claim_ids=[buyer_claim_id],
        extracted_facts=[
            {"field": "promised_memory_gb", "value": scenario["promised_memory_gb"]},
            {"field": "listing_serial", "value": scenario["listing_serial"]},
        ],
    )

    report = scenario.get("buyer_report")
    if report:
        _add_evidence(
            session,
            evidence_id=f"ev_{slug}_buyer_report",
            dispute_id=dispute_id,
            submitted_by="BUYER",
            evidence_type="DOCUMENT",
            description=f"买方提交设备信息文本记录：内存 {report['memory_gb']}GB，序列号 {report['serial']}，来源 {report['quality']}。",
            source_system="EVIDENCE_STORE",
            submitted_at=delivered_at + timedelta(hours=20),
            related_claim_ids=[buyer_claim_id] + ([seller_claim_id] if seller_claim_id else []),
            extracted_facts=[
                {"field": "detected_memory_gb", "value": report["memory_gb"]},
                {"field": "detected_serial", "value": report["serial"]},
                {"field": "quality", "value": report["quality"]},
            ],
        )

    seller_report = scenario.get("seller_report")
    if seller_report and seller_claim_id:
        _add_evidence(
            session,
            evidence_id=f"ev_{slug}_seller_report",
            dispute_id=dispute_id,
            submitted_by="SELLER",
            evidence_type="DOCUMENT",
            description=f"卖方发货前设备信息文本记录：内存 {seller_report['memory_gb']}GB，序列号 {seller_report['serial']}。",
            source_system="EVIDENCE_STORE",
            submitted_at=paid_at + timedelta(hours=8),
            related_claim_ids=[buyer_claim_id, seller_claim_id],
            extracted_facts=[
                {"field": "preshipment_memory_gb", "value": seller_report["memory_gb"]},
                {"field": "preshipment_serial", "value": seller_report["serial"]},
                {"field": "quality", "value": seller_report["quality"]},
            ],
        )

    if scenario.get("appealed"):
        _seed_appeal_history(session, dispute, slug)


def _ensure_text_only_demo_materials(session: Session, scenario: dict[str, Any]) -> None:
    """Keep seeded demos text-first and enrich existing databases idempotently."""
    slug = scenario["slug"]
    dispute = session.get(Dispute, f"case_{slug}")
    if dispute is None:
        return
    transaction = session.get(Transaction, dispute.transaction_id)
    if transaction is None or transaction.delivered_at is None:
        return

    conversations: dict[str, tuple[str, str]] = {
        "clear_mismatch": (
            "我刚签收，平台设备信息文本导出显示内存为 8GB，设备序列号是 SN-CLEAR-001，与订单一致但和你承诺的 16GB 不符。",
            "我看到了你发来的文字检测记录。页面和交易前聊天写的是 16GB；我发货前没有另做配置检测，也没有提出调包，按平台规则处理。",
        ),
        "buyer_missing_evidence": (
            "我感觉收到的电脑配置不对，但目前还没有导出系统设备信息。",
            "请先提供能同时显示内存和设备序列号的文字检测记录，我再回应。",
        ),
        "seller_preshipment": (
            "签收后的设备信息文本显示 8GB，序列号是 SN-SELLER-001。",
            "我保留了发货前设备信息文本日志，当时记录为 16GB、SN-SELLER-001，请平台核对形成时间。",
        ),
        "serial_conflict": (
            "签收后文字检测报告显示 8GB，但序列号是 SN-RECEIVED-999。",
            "发布记录是 SN-ORIGINAL-001，我对设备身份有异议，请平台人工核验。",
        ),
        "appeal_reversal": (
            "我补交第三方文字检测原报告，显示 8GB 和 SN-APPEAL-001。",
            "我已看到新报告，请平台按申诉规则重新审查。",
        ),
    }
    buyer_body, seller_body = conversations[slug]
    message_specs = [
        (
            f"message_{slug}_buyer_text_claim",
            "user_buyer_demo",
            "BUYER",
            buyer_body,
            transaction.delivered_at + timedelta(hours=17, minutes=10),
        ),
        (
            f"message_{slug}_seller_text_response",
            "user_seller_demo",
            "SELLER",
            seller_body,
            transaction.delivered_at + timedelta(hours=17, minutes=25),
        ),
    ]
    for message_id, sender_id, sender_role, body, sent_at in message_specs:
        if session.get(Message, message_id) is None:
            session.add(
                Message(
                    id=message_id,
                    transaction_id=transaction.id,
                    dispute_id=dispute.id,
                    sender_id=sender_id,
                    sender_role=sender_role,
                    body=body,
                    sent_at=sent_at,
                    content_sha256=digest(body),
                    snapshot_locked=True,
                )
            )

    report_spec = scenario.get("buyer_report")
    buyer_report = session.get(Evidence, f"ev_{slug}_buyer_report")
    if buyer_report is not None and report_spec:
        description = (
            f"买方提交设备信息文本记录：内存 {report_spec['memory_gb']}GB，"
            f"序列号 {report_spec['serial']}，来源 {report_spec['quality']}。"
        )
        buyer_report.evidence_type = "DOCUMENT"
        buyer_report.description = description
        buyer_report.content_sha256 = digest(f"{buyer_report.id}:{description}")
        buyer_report.extracted_facts_json = [
            {"field": "detected_memory_gb", "value": report_spec["memory_gb"]},
            {"field": "detected_serial", "value": report_spec["serial"]},
            {"field": "quality", "value": report_spec["quality"]},
        ]
        if report_spec["quality"] == "PLATFORM_TEXT_EXPORT":
            buyer_report.source_system = "PLATFORM_DEVICE_DIAGNOSTIC"
            buyer_report.integrity_status = "SOURCE_VERIFIED"

    seller_spec = scenario.get("seller_report")
    seller_report = session.get(Evidence, f"ev_{slug}_seller_report")
    if seller_report is not None and seller_spec:
        description = (
            f"卖方发货前设备信息文本记录：内存 {seller_spec['memory_gb']}GB，"
            f"序列号 {seller_spec['serial']}。"
        )
        seller_report.evidence_type = "DOCUMENT"
        seller_report.description = description
        seller_report.content_sha256 = digest(f"{seller_report.id}:{description}")
        seller_report.extracted_facts_json = [
            {"field": "preshipment_memory_gb", "value": seller_spec["memory_gb"]},
            {"field": "preshipment_serial", "value": seller_spec["serial"]},
            {"field": "quality", "value": seller_spec["quality"]},
        ]


def seed_database(session: Session, policy_directory: Path | None = None) -> dict[str, int]:
    policy_directory = policy_directory or get_settings().policy_directory
    _ensure_users(session)
    session.flush()
    _load_policy_versions(session, policy_directory)
    for index, scenario in enumerate(SCENARIOS, start=1):
        _seed_scenario(session, scenario, index)
        _ensure_text_only_demo_materials(session, scenario)
    session.commit()
    return {
        "users": session.scalar(select(func.count()).select_from(User)) or 0,
        "transactions": session.scalar(select(func.count()).select_from(Transaction)) or 0,
        "disputes": session.scalar(select(func.count()).select_from(Dispute)) or 0,
        "policy_versions": session.scalar(select(func.count()).select_from(PolicyVersion)) or 0,
        "seeded_at": int(utc_now().timestamp()),
    }


def main() -> None:
    with SessionLocal() as session:
        counts = seed_database(session)
    print(json.dumps(counts, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
