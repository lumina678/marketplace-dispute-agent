from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import select

from dispute_agent.api import create_app
from dispute_agent.models import Message


PAID_AT = "2026-07-10T09:30:00+08:00"
DELIVERED_AT = "2026-07-13T15:30:00+08:00"
CAPTURED_AT = "2026-07-13T16:00:00+08:00"
ASSERTED_AT = "2026-07-13T18:00:00+08:00"


def app_for(context):  # type: ignore[no-untyped-def]
    settings = context.settings.model_copy(update={"default_tool_call_budget": 60})
    return create_app(context.sessions, settings=settings)


async def import_base_case(
    client: httpx.AsyncClient,
    *,
    suffix: str,
    dispute_type: str = "DESCRIPTION_MISMATCH",
    claim_type: str = "CONFIG_MISMATCH",
    statement: str = "卖方承诺 16GB，买方收到后检测为 8GB。",
) -> dict[str, str]:
    ids = {
        "transaction_id": f"txn_intake_{suffix}",
        "listing_id": f"listing_intake_{suffix}",
        "snapshot_id": f"snapshot_intake_{suffix}",
        "message_id": f"message_intake_{suffix}",
        "case_id": f"case_intake_{suffix}",
        "claim_id": f"claim_intake_{suffix}",
    }
    transaction = await client.post(
        "/transactions",
        json={
            "transaction_id": ids["transaction_id"],
            "buyer_id": "user_buyer_demo",
            "seller_id": "user_seller_demo",
            "listing_id": ids["listing_id"],
            "category": "USED_LAPTOP",
            "paid_amount_minor": 280000,
            "paid_at": PAID_AT,
            "delivered_at": DELIVERED_AT,
            "order_status": "DISPUTED",
            "funds_status": "HELD",
            "actor_id": "user_admin_demo",
        },
    )
    assert transaction.status_code == 200, transaction.text
    assert transaction.json()["created"] is True

    snapshot = await client.post(
        f"/transactions/{ids['transaction_id']}/listing-snapshots",
        json={
            "snapshot_id": ids["snapshot_id"],
            "payload": {
                "title": "通用接入二手笔记本",
                "category": "USED_LAPTOP",
                "memory_gb": 16,
                "device_serial": f"SN-{suffix.upper()}",
                "included_items": ["笔记本", "充电器"],
                "condition": "功能正常",
            },
            "captured_at": CAPTURED_AT,
            "actor_id": "user_admin_demo",
        },
    )
    assert snapshot.status_code == 200, snapshot.text

    messages = await client.post(
        f"/transactions/{ids['transaction_id']}/messages:batch",
        json={
            "actor_id": "user_admin_demo",
            "messages": [
                {
                    "message_id": ids["message_id"],
                    "sender_role": "SELLER",
                    "body": "确认是 16GB 内存，功能正常，带原装充电器。",
                    "sent_at": "2026-07-10T08:30:00+08:00",
                }
            ],
        },
    )
    assert messages.status_code == 200, messages.text

    dispute = await client.post(
        "/disputes",
        json={
            "case_id": ids["case_id"],
            "transaction_id": ids["transaction_id"],
            "dispute_type": dispute_type,
            "submitted_by_id": "user_buyer_demo",
            "claims": [
                {
                    "claim_id": ids["claim_id"],
                    "party": "BUYER",
                    "claim_type": claim_type,
                    "statement": statement,
                    "asserted_at": ASSERTED_AT,
                }
            ],
        },
    )
    assert dispute.status_code == 200, dispute.text
    assert dispute.json()["routing"]["case_routing"]["ready_for_investigation"] is True
    return ids


def test_complete_generic_intake_freezes_and_runs_agent_workflow(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app_for(context))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ids = await import_base_case(client, suffix="happy")
            evidence = await client.post(
                f"/cases/{ids['case_id']}/evidence",
                json={
                    "evidence_id": "ev_intake_happy_buyer_report",
                    "submitted_by": "BUYER",
                    "submitter_id": "user_buyer_demo",
                    "evidence_type": "DEVICE_REPORT",
                    "description": "买方设备信息文字导出",
                    "text_content": "设备序列号 SN-HAPPY，系统检测内存 8GB。",
                    "source_record_id": "buyer_report_happy",
                    "captured_at": "2026-07-13T17:00:00+08:00",
                    "related_claim_ids": [ids["claim_id"]],
                    "extracted_facts": [
                        {"field": "detected_memory_gb", "value": 8},
                        {"field": "detected_serial", "value": "SN-HAPPY"},
                        {"field": "quality", "value": "PLATFORM_TEXT_EXPORT"},
                    ],
                },
            )
            assert evidence.status_code == 200, evidence.text
            assert evidence.json()["created"] is True
            evidence_replay = await client.post(
                f"/cases/{ids['case_id']}/evidence",
                json={
                    "evidence_id": "ev_intake_happy_buyer_report",
                    "submitted_by": "BUYER",
                    "submitter_id": "user_buyer_demo",
                    "evidence_type": "DEVICE_REPORT",
                    "description": "买方设备信息文字导出",
                    "text_content": "设备序列号 SN-HAPPY，系统检测内存 8GB。",
                    "source_record_id": "buyer_report_happy",
                    "captured_at": "2026-07-13T17:00:00+08:00",
                    "related_claim_ids": [ids["claim_id"]],
                    "extracted_facts": [
                        {"field": "detected_memory_gb", "value": 8},
                        {"field": "detected_serial", "value": "SN-HAPPY"},
                        {"field": "quality", "value": "PLATFORM_TEXT_EXPORT"},
                    ],
                },
            )
            assert evidence_replay.status_code == 200
            assert evidence_replay.json()["created"] is False

            frozen = await client.post(
                f"/cases/{ids['case_id']}/freeze",
                json={"actor_id": "user_admin_demo", "expected_state_version": 1},
            )
            assert frozen.status_code == 200, frozen.text
            assert frozen.json()["state"] == "EVIDENCE_LOCKED"
            assert frozen.json()["manifest_integrity_valid"] is True
            assert frozen.json()["policy"] == {
                "policy_id": "marketplace.description_mismatch",
                "version": "2.0.0",
            }
            manifest_hash = frozen.json()["intake_manifest_sha256"]

            replay = await client.post(
                f"/cases/{ids['case_id']}/freeze",
                json={"actor_id": "user_admin_demo", "expected_state_version": 1},
            )
            assert replay.status_code == 200
            assert replay.json()["idempotent_replay"] is True
            assert replay.json()["intake_manifest_sha256"] == manifest_hash

            intake = await client.get(f"/cases/{ids['case_id']}/intake")
            assert intake.status_code == 200
            view = intake.json()
            assert view["freeze"]["manifest_integrity_valid"] is True
            assert all(item["snapshot_locked"] for item in view["messages"])
            assert {item["evidence_type"] for item in view["evidence"]} >= {
                "DEVICE_REPORT",
                "LISTING_SNAPSHOT",
                "CHAT_SNAPSHOT",
            }

            inspection = await client.post(
                "/tools/evidence.inspect",
                json={
                    "actor": "REVIEWER",
                    "parameters": {
                        "case_id": ids["case_id"],
                        "evidence_id": "ev_intake_happy_buyer_report",
                    },
                },
            )
            assert inspection.status_code == 200
            assert "系统检测内存 8GB" in inspection.json()["text_content"]

            workflow = await client.post(f"/cases/{ids['case_id']}/workflow", json={})
            assert workflow.status_code == 202, workflow.text
            assert workflow.json()["status"] == "COMPLETED"
            assert workflow.json()["result"]["workflow_boundary"] == "HUMAN_REVIEW_REQUIRED"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("suffix", "dispute_type", "claim_type", "policy_id"),
    [
        ("missing", "MISSING_PARTS", "MISSING_ACCESSORY", "marketplace.missing_parts"),
        ("empty", "EMPTY_PACKAGE", "PACKAGE_EMPTY", "marketplace.empty_package"),
        ("damage", "SHIPPING_DAMAGE", "ITEM_DAMAGED_IN_TRANSIT", "marketplace.shipping_damage"),
    ],
)
def test_generic_intake_is_not_hardcoded_to_description_mismatch(
    context,
    suffix: str,
    dispute_type: str,
    claim_type: str,
    policy_id: str,
) -> None:
    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app_for(context))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ids = await import_base_case(
                client,
                suffix=suffix,
                dispute_type=dispute_type,
                claim_type=claim_type,
                statement=f"通用接入测试：{dispute_type}",
            )
            frozen = await client.post(
                f"/cases/{ids['case_id']}/freeze",
                json={"actor_id": "user_admin_demo", "expected_state_version": 1},
            )
            assert frozen.status_code == 200, frozen.text
            assert frozen.json()["policy"]["policy_id"] == policy_id
            routing = await client.get(f"/cases/{ids['case_id']}/routing")
            assert routing.json()["claims"][0]["issue_type"] == dispute_type
            assert routing.json()["claims"][0]["routing_status"] == "ROUTED"

    asyncio.run(exercise())


def test_frozen_materials_reject_baseline_mutation(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app_for(context))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ids = await import_base_case(client, suffix="locked")
            frozen = await client.post(
                f"/cases/{ids['case_id']}/freeze",
                json={"actor_id": "user_admin_demo", "expected_state_version": 1},
            )
            assert frozen.status_code == 200

            replay_snapshot = await client.post(
                f"/transactions/{ids['transaction_id']}/listing-snapshots",
                json={
                    "snapshot_id": ids["snapshot_id"],
                    "payload": {
                        "title": "通用接入二手笔记本",
                        "category": "USED_LAPTOP",
                        "memory_gb": 16,
                        "device_serial": "SN-LOCKED",
                        "included_items": ["笔记本", "充电器"],
                        "condition": "功能正常",
                    },
                    "captured_at": CAPTURED_AT,
                    "actor_id": "user_admin_demo",
                },
            )
            assert replay_snapshot.status_code == 200
            assert replay_snapshot.json()["created"] is False
            replay_chat = await client.post(
                f"/transactions/{ids['transaction_id']}/messages:batch",
                json={
                    "actor_id": "user_admin_demo",
                    "messages": [
                        {
                            "message_id": ids["message_id"],
                            "sender_role": "SELLER",
                            "body": "确认是 16GB 内存，功能正常，带原装充电器。",
                            "sent_at": "2026-07-10T08:30:00+08:00",
                        }
                    ],
                },
            )
            assert replay_chat.status_code == 200
            assert replay_chat.json()["reused_count"] == 1

            snapshot = await client.post(
                f"/transactions/{ids['transaction_id']}/listing-snapshots",
                json={
                    "snapshot_id": "snapshot_after_freeze",
                    "payload": {"memory_gb": 32},
                    "captured_at": CAPTURED_AT,
                    "actor_id": "user_admin_demo",
                },
            )
            assert snapshot.status_code == 409

            chat = await client.post(
                f"/transactions/{ids['transaction_id']}/messages:batch",
                json={
                    "actor_id": "user_admin_demo",
                    "messages": [
                        {
                            "message_id": "message_after_freeze",
                            "sender_role": "SELLER",
                            "body": "试图修改冻结聊天",
                            "sent_at": "2026-07-10T08:35:00+08:00",
                        }
                    ],
                },
            )
            assert chat.status_code == 409

            evidence = await client.post(
                f"/cases/{ids['case_id']}/evidence",
                json={
                    "evidence_id": "ev_after_freeze",
                    "submitted_by": "BUYER",
                    "submitter_id": "user_buyer_demo",
                    "evidence_type": "PARTY_STATEMENT",
                    "description": "冻结后直接追加",
                    "text_content": "这条材料应当被拒绝。",
                    "source_record_id": "after_freeze",
                    "captured_at": CAPTURED_AT,
                    "related_claim_ids": [ids["claim_id"]],
                },
            )
            assert evidence.status_code == 409
            assert "补问或申诉接口" in evidence.json()["error"]["message"]

    asyncio.run(exercise())


def test_intake_writes_are_idempotent_and_chat_batch_is_atomic(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app_for(context))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ids = await import_base_case(client, suffix="idempotent")
            transaction_payload = {
                "transaction_id": ids["transaction_id"],
                "buyer_id": "user_buyer_demo",
                "seller_id": "user_seller_demo",
                "listing_id": ids["listing_id"],
                "category": "USED_LAPTOP",
                "paid_amount_minor": 280000,
                "paid_at": PAID_AT,
                "delivered_at": DELIVERED_AT,
                "order_status": "DISPUTED",
                "funds_status": "HELD",
                "actor_id": "user_admin_demo",
            }
            replay_transaction = await client.post("/transactions", json=transaction_payload)
            assert replay_transaction.status_code == 200
            assert replay_transaction.json()["created"] is False
            conflicting_transaction = await client.post(
                "/transactions",
                json={**transaction_payload, "paid_amount_minor": 1},
            )
            assert conflicting_transaction.status_code == 409

            replay_snapshot = await client.post(
                f"/transactions/{ids['transaction_id']}/listing-snapshots",
                json={
                    "snapshot_id": ids["snapshot_id"],
                    "payload": {
                        "title": "通用接入二手笔记本",
                        "category": "USED_LAPTOP",
                        "memory_gb": 16,
                        "device_serial": "SN-IDEMPOTENT",
                        "included_items": ["笔记本", "充电器"],
                        "condition": "功能正常",
                    },
                    "captured_at": CAPTURED_AT,
                    "actor_id": "user_admin_demo",
                },
            )
            assert replay_snapshot.status_code == 200
            assert replay_snapshot.json()["created"] is False

            replay_chat = await client.post(
                f"/transactions/{ids['transaction_id']}/messages:batch",
                json={
                    "actor_id": "user_admin_demo",
                    "messages": [
                        {
                            "message_id": ids["message_id"],
                            "sender_role": "SELLER",
                            "body": "确认是 16GB 内存，功能正常，带原装充电器。",
                            "sent_at": "2026-07-10T08:30:00+08:00",
                        }
                    ],
                },
            )
            assert replay_chat.status_code == 200
            assert replay_chat.json()["reused_count"] == 1

            atomic_conflict = await client.post(
                f"/transactions/{ids['transaction_id']}/messages:batch",
                json={
                    "actor_id": "user_admin_demo",
                    "messages": [
                        {
                            "message_id": ids["message_id"],
                            "sender_role": "SELLER",
                            "body": "相同 ID 的不同内容",
                            "sent_at": "2026-07-10T08:30:00+08:00",
                        },
                        {
                            "message_id": "message_should_not_commit",
                            "sender_role": "BUYER",
                            "body": "整批应回滚",
                            "sent_at": "2026-07-10T08:31:00+08:00",
                        },
                    ],
                },
            )
            assert atomic_conflict.status_code == 409

            replay_dispute = await client.post(
                "/disputes",
                json={
                    "case_id": ids["case_id"],
                    "transaction_id": ids["transaction_id"],
                    "dispute_type": "DESCRIPTION_MISMATCH",
                    "submitted_by_id": "user_buyer_demo",
                    "claims": [
                        {
                            "claim_id": ids["claim_id"],
                            "party": "BUYER",
                            "claim_type": "CONFIG_MISMATCH",
                            "statement": "卖方承诺 16GB，买方收到后检测为 8GB。",
                            "asserted_at": ASSERTED_AT,
                        }
                    ],
                },
            )
            assert replay_dispute.status_code == 200
            assert replay_dispute.json()["created"] is False
            intake = await client.get(f"/cases/{ids['case_id']}/intake")
            assert [item["message_id"] for item in intake.json()["messages"]] == [ids["message_id"]]

    asyncio.run(exercise())


def test_intake_enforces_authorization_hashes_and_freeze_preconditions(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app_for(context))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            forbidden = await client.post(
                "/transactions",
                json={
                    "transaction_id": "txn_intake_forbidden",
                    "buyer_id": "user_buyer_demo",
                    "seller_id": "user_seller_demo",
                    "listing_id": "listing_intake_forbidden",
                    "paid_amount_minor": 10000,
                    "paid_at": PAID_AT,
                    "actor_id": "user_buyer_demo",
                },
            )
            assert forbidden.status_code == 403

            ids = await import_base_case(client, suffix="guards")
            bad_hash = await client.post(
                f"/cases/{ids['case_id']}/evidence",
                json={
                    "evidence_id": "ev_bad_hash",
                    "submitted_by": "BUYER",
                    "submitter_id": "user_buyer_demo",
                    "evidence_type": "PARTY_STATEMENT",
                    "description": "哈希不匹配",
                    "text_content": "证据原文",
                    "source_record_id": "bad_hash",
                    "captured_at": CAPTURED_AT,
                    "related_claim_ids": [ids["claim_id"]],
                    "content_sha256": "0" * 64,
                },
            )
            assert bad_hash.status_code == 400

            wrong_version = await client.post(
                f"/cases/{ids['case_id']}/freeze",
                json={"actor_id": "user_admin_demo", "expected_state_version": 99},
            )
            assert wrong_version.status_code == 409
            intake = await client.get(f"/cases/{ids['case_id']}/intake")
            assert intake.json()["state"] == "SUBMITTED"
            assert intake.json()["freeze"]["frozen"] is False

    asyncio.run(exercise())


def test_freeze_requires_chat_snapshot(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app_for(context))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ids = {
                "transaction_id": "txn_intake_no_chat",
                "case_id": "case_intake_no_chat",
                "claim_id": "claim_intake_no_chat",
            }
            transaction = await client.post(
                "/transactions",
                json={
                    "transaction_id": ids["transaction_id"],
                    "buyer_id": "user_buyer_demo",
                    "seller_id": "user_seller_demo",
                    "listing_id": "listing_intake_no_chat",
                    "paid_amount_minor": 20000,
                    "paid_at": PAID_AT,
                    "actor_id": "user_admin_demo",
                },
            )
            assert transaction.status_code == 200
            snapshot = await client.post(
                f"/transactions/{ids['transaction_id']}/listing-snapshots",
                json={
                    "snapshot_id": "snapshot_intake_no_chat",
                    "payload": {"memory_gb": 16, "device_serial": "SN-NO-CHAT"},
                    "captured_at": CAPTURED_AT,
                    "actor_id": "user_admin_demo",
                },
            )
            assert snapshot.status_code == 200
            dispute = await client.post(
                "/disputes",
                json={
                    "case_id": ids["case_id"],
                    "transaction_id": ids["transaction_id"],
                    "dispute_type": "DESCRIPTION_MISMATCH",
                    "submitted_by_id": "user_buyer_demo",
                    "claims": [
                        {
                            "claim_id": ids["claim_id"],
                            "party": "BUYER",
                            "claim_type": "CONFIG_MISMATCH",
                            "statement": "配置不符",
                            "asserted_at": ASSERTED_AT,
                        }
                    ],
                },
            )
            assert dispute.status_code == 200
            frozen = await client.post(
                f"/cases/{ids['case_id']}/freeze",
                json={"actor_id": "user_admin_demo", "expected_state_version": 1},
            )
            assert frozen.status_code == 400
            assert "至少一条交易聊天" in frozen.json()["error"]["message"]

    asyncio.run(exercise())


def test_manifest_detects_out_of_band_material_tampering(context) -> None:  # type: ignore[no-untyped-def]
    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app_for(context))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ids = await import_base_case(client, suffix="tamper")
            frozen = await client.post(
                f"/cases/{ids['case_id']}/freeze",
                json={"actor_id": "user_admin_demo", "expected_state_version": 1},
            )
            assert frozen.status_code == 200
            assert frozen.json()["manifest_integrity_valid"] is True

            with context.sessions() as session:
                message = session.scalar(
                    select(Message).where(Message.id == ids["message_id"])
                )
                assert message is not None
                message.body = "绕过 API 直接篡改数据库中的聊天正文"
                session.commit()

            intake = await client.get(f"/cases/{ids['case_id']}/intake")
            assert intake.status_code == 200
            assert intake.json()["freeze"]["manifest_integrity_valid"] is False

    asyncio.run(exercise())
