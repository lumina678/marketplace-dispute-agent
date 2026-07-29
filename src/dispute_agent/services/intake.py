from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.db import SessionLocal
from dispute_agent.dispute_types import RoutingSource, RoutingStatus
from dispute_agent.errors import AuthorizationError, ConflictError, NotFoundError, ValidationError
from dispute_agent.intake_schemas import (
    ChatBatchImportRequest,
    DisputeSubmitRequest,
    FreezeCaseMaterialsRequest,
    ListingSnapshotImportRequest,
    TextEvidenceUploadRequest,
    TransactionCreateRequest,
)
from dispute_agent.models import (
    Claim,
    Dispute,
    Evidence,
    ListingSnapshot,
    Message,
    Transaction,
    User,
    utc_now,
)
from dispute_agent.routing_schemas import ClaimRoutingHint
from dispute_agent.serialization import canonical_json, content_hash, jsonable
from dispute_agent.services.claim_router import ClaimRoutingService
from dispute_agent.services.policy import PolicyService
from dispute_agent.services.state_machine import StateMachineService
from dispute_agent.skills import SkillRegistry, get_skill_registry


class CaseIntakeService:
    """Idempotent write boundary for synthetic transactions and dispute materials."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        routing_service: ClaimRoutingService | None = None,
        state_machine: StateMachineService | None = None,
        skill_registry: SkillRegistry | None = None,
    ):
        self.session_factory = session_factory
        self.skill_registry = skill_registry or get_skill_registry()
        self.routing_service = routing_service or ClaimRoutingService(
            session_factory,
            skill_registry=self.skill_registry,
        )
        self.state_machine = state_machine or StateMachineService()

    def create_transaction(self, request: TransactionCreateRequest) -> dict[str, Any]:
        with self.session_factory() as session:
            self._require_actor(session, request.actor_id, {"ADMIN", "REVIEWER"})
            buyer = session.get(User, request.buyer_id)
            seller = session.get(User, request.seller_id)
            if buyer is None or buyer.role != "BUYER":
                raise ValidationError("buyer_id 必须引用已登记的 BUYER")
            if seller is None or seller.role != "SELLER":
                raise ValidationError("seller_id 必须引用已登记的 SELLER")

            expected = request.model_dump(exclude={"actor_id"})
            existing = session.get(Transaction, request.transaction_id)
            if existing is not None:
                actual = self._transaction_payload(existing)
                if actual != expected:
                    raise ConflictError("transaction_id 已存在，但请求内容与已保存交易不一致")
                return {"created": False, "transaction": jsonable(actual)}

            transaction = Transaction(
                id=request.transaction_id,
                buyer_id=request.buyer_id,
                seller_id=request.seller_id,
                listing_id=request.listing_id,
                category=request.category,
                paid_amount_minor=request.paid_amount_minor,
                currency=request.currency,
                paid_at=request.paid_at,
                delivered_at=request.delivered_at,
                order_status=request.order_status,
                funds_status=request.funds_status,
            )
            session.add(transaction)
            session.commit()
            return {"created": True, "transaction": jsonable(self._transaction_payload(transaction))}

    def import_listing_snapshot(
        self,
        transaction_id: str,
        request: ListingSnapshotImportRequest,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            self._require_actor(session, request.actor_id, {"ADMIN", "REVIEWER"})
            transaction = self._require_transaction(session, transaction_id)
            digest = content_hash(request.payload)
            self._verify_digest(request.content_sha256, digest, "商品快照")
            existing_by_id = session.get(ListingSnapshot, request.snapshot_id)
            existing_by_type = session.scalar(
                select(ListingSnapshot).where(
                    ListingSnapshot.transaction_id == transaction_id,
                    ListingSnapshot.snapshot_type == request.snapshot_type,
                )
            )
            existing = existing_by_id or existing_by_type
            if existing is not None:
                if (
                    existing.transaction_id != transaction_id
                    or existing.snapshot_type != request.snapshot_type
                    or existing.content_sha256 != digest
                    or existing.captured_at != request.captured_at
                ):
                    raise ConflictError("商品快照 ID 或基线类型已存在，但内容不一致")
                return {"created": False, "snapshot": jsonable(self._snapshot_payload(existing))}

            self._require_transaction_materials_mutable(session, transaction_id)
            snapshot = ListingSnapshot(
                id=request.snapshot_id,
                transaction_id=transaction_id,
                listing_id=transaction.listing_id,
                snapshot_type=request.snapshot_type,
                payload_json=request.payload,
                content_sha256=digest,
                immutable_uri=request.immutable_uri or f"snapshot://listing/{transaction.listing_id}/{request.snapshot_id}",
                captured_at=request.captured_at,
            )
            session.add(snapshot)
            session.commit()
            return {"created": True, "snapshot": jsonable(self._snapshot_payload(snapshot))}

    def import_chat_batch(
        self,
        transaction_id: str,
        request: ChatBatchImportRequest,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            self._require_actor(session, request.actor_id, {"ADMIN", "REVIEWER"})
            transaction = self._require_transaction(session, transaction_id)
            prepared: list[tuple[Any, str, str]] = []
            created_ids: list[str] = []
            reused_ids: list[str] = []
            for item in request.messages:
                sender_id = transaction.buyer_id if item.sender_role == "BUYER" else transaction.seller_id
                digest = content_hash(
                    {
                        "transaction_id": transaction_id,
                        "message_id": item.message_id,
                        "sender_id": sender_id,
                        "sender_role": item.sender_role,
                        "body": item.body,
                        "sent_at": item.sent_at,
                    }
                )
                self._verify_digest(item.content_sha256, digest, f"聊天 {item.message_id}")
                existing = session.get(Message, item.message_id)
                if existing is not None:
                    if (
                        existing.transaction_id != transaction_id
                        or existing.sender_id != sender_id
                        or existing.sender_role != item.sender_role
                        or existing.body != item.body
                        or existing.sent_at != item.sent_at
                        or existing.content_sha256 != digest
                    ):
                        raise ConflictError(f"message_id={item.message_id} 已存在，但内容不一致")
                    reused_ids.append(existing.id)
                    continue
                prepared.append((item, sender_id, digest))

            if prepared:
                self._require_transaction_materials_mutable(session, transaction_id)
            for item, sender_id, digest in prepared:
                message = Message(
                    id=item.message_id,
                    transaction_id=transaction_id,
                    dispute_id=None,
                    sender_id=sender_id,
                    sender_role=item.sender_role,
                    body=item.body,
                    sent_at=item.sent_at,
                    content_sha256=digest,
                    snapshot_locked=False,
                )
                session.add(message)
                created_ids.append(message.id)
            session.commit()
            return {
                "transaction_id": transaction_id,
                "created_count": len(created_ids),
                "reused_count": len(reused_ids),
                "created_message_ids": created_ids,
                "reused_message_ids": reused_ids,
                "snapshot_locked": False,
            }

    def submit_dispute(
        self,
        request: DisputeSubmitRequest,
        *,
        recorded_by_id: str | None = None,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            transaction = self._require_transaction(session, request.transaction_id)
            recorder = (
                self._require_actor(session, recorded_by_id, {"REVIEWER", "ADMIN"})
                if recorded_by_id
                else None
            )
            actor = self._require_actor(session, request.submitted_by_id, {"BUYER", "SELLER", "ADMIN"})
            if actor.role != "ADMIN":
                participant_id = transaction.buyer_id if actor.role == "BUYER" else transaction.seller_id
                if actor.id != participant_id:
                    raise AuthorizationError("只有交易参与方可以提交该交易的争议")
                if any(item.party != actor.role for item in request.claims):
                    raise AuthorizationError("普通交易参与方只能提交本方 Claim")

            for item in request.claims:
                skills = self.skill_registry.for_dispute_type(request.dispute_type.value)
                if skills and not any(
                    skill.supports(request.dispute_type.value, item.claim_type) for skill in skills
                ):
                    raise ValidationError(
                        f"claim_type={item.claim_type} 不受 {request.dispute_type.value} 的已注册 Skill 支持"
                    )

            existing = session.get(Dispute, request.case_id)
            if existing is not None:
                if not self._dispute_matches_request(session, existing, request):
                    raise ConflictError("case_id 已存在，但争议或 Claim 内容不一致")
                created = False
                current_state = existing.state
                route_required = any(
                    item.routing_status == RoutingStatus.UNROUTED.value
                    for item in session.scalars(
                        select(Claim).where(Claim.dispute_id == request.case_id)
                    )
                )
            else:
                other = session.scalar(
                    select(Dispute).where(Dispute.transaction_id == request.transaction_id)
                )
                if other is not None:
                    raise ConflictError(f"交易已经存在争议案件: {other.id}")
                dispute = Dispute(
                    id=request.case_id,
                    transaction_id=request.transaction_id,
                    recorded_by_id=recorder.id if recorder else None,
                    dispute_type=request.dispute_type.value,
                    state="SUBMITTED",
                    state_version=1,
                    question_round_count=0,
                    requires_human_review=True,
                    human_review_reasons_json=[],
                )
                session.add(dispute)
                session.flush()
                claim_by_id: dict[str, Claim] = {}
                for item in request.claims:
                    claim = Claim(
                        id=item.claim_id,
                        dispute_id=request.case_id,
                        party=item.party,
                        claim_type=item.claim_type,
                        issue_type=request.dispute_type.value,
                        issue_subtype=item.claim_type,
                        routing_source=RoutingSource.USER_DECLARED.value,
                        routing_reason=None,
                        routing_confidence=None,
                        skill_name=None,
                        skill_version=None,
                        routing_status=RoutingStatus.UNROUTED.value,
                        statement=item.statement,
                        status="ALLEGED",
                        material=item.material,
                        responds_to_claim_id=None,
                        asserted_at=item.asserted_at,
                    )
                    session.add(claim)
                    claim_by_id[item.claim_id] = claim
                session.flush()
                for item in request.claims:
                    if item.responds_to_claim_id:
                        claim_by_id[item.claim_id].responds_to_claim_id = item.responds_to_claim_id
                session.commit()
                created = True
                current_state = "SUBMITTED"
                route_required = True

        if route_required:
            hints = {
                item.claim_id: ClaimRoutingHint(
                    declared_issue_type=request.dispute_type,
                    declared_claim_type=item.claim_type,
                )
                for item in request.claims
            }
            routing = self.routing_service.route_case(
                request.case_id,
                hints=hints,
                actor_id=recorded_by_id or request.submitted_by_id,
                force_recompute=False,
            )
        else:
            routing = self.routing_service.get_case_routing(request.case_id)
        return {
            "created": created,
            "case_id": request.case_id,
            "state": current_state,
            "routing": routing,
        }

    def upload_text_evidence(
        self,
        case_id: str,
        request: TextEvidenceUploadRequest,
        *,
        recorded_by_id: str | None = None,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute = self._require_dispute(session, case_id)
            transaction = dispute.transaction
            recorder = (
                self._require_actor(session, recorded_by_id, {"REVIEWER", "ADMIN"})
                if recorded_by_id
                else None
            )
            actor = self._require_actor(session, request.submitter_id, {"BUYER", "SELLER", "REVIEWER", "ADMIN"})
            if request.submitted_by in {"BUYER", "SELLER"}:
                participant_id = (
                    transaction.buyer_id if request.submitted_by == "BUYER" else transaction.seller_id
                )
                if actor.id != participant_id or actor.role != request.submitted_by:
                    raise AuthorizationError("买卖双方证据必须绑定真实交易参与方")
            elif actor.role in {"BUYER", "SELLER"}:
                raise AuthorizationError("交易参与方只能以本方身份提交证据")
            elif actor.role == "REVIEWER" and request.submitted_by != "REVIEWER" and recorder is None:
                raise AuthorizationError("REVIEWER 只能以 REVIEWER 身份提交证据")
            elif actor.role == "ADMIN" and request.submitted_by not in {"SYSTEM", "THIRD_PARTY", "REVIEWER"}:
                raise AuthorizationError("ADMIN 只能导入 SYSTEM、THIRD_PARTY 或 REVIEWER 证据")

            known_claim_ids = set(
                session.scalars(select(Claim.id).where(Claim.dispute_id == case_id))
            )
            if not set(request.related_claim_ids).issubset(known_claim_ids):
                raise ValidationError("related_claim_ids 必须全部属于当前案件")
            digest = content_hash(
                {
                    "case_id": case_id,
                    "evidence_type": request.evidence_type,
                    "description": request.description,
                    "text_content": request.text_content,
                    "source_system": request.source_system,
                    "source_record_id": request.source_record_id,
                    "captured_at": request.captured_at,
                    "related_claim_ids": sorted(request.related_claim_ids),
                    "extracted_facts": request.extracted_facts,
                }
            )
            self._verify_digest(request.content_sha256, digest, "文字证据")
            existing_by_id = session.get(Evidence, request.evidence_id)
            existing_by_hash = session.scalar(
                select(Evidence).where(
                    Evidence.dispute_id == case_id,
                    Evidence.content_sha256 == digest,
                )
            )
            existing = existing_by_id or existing_by_hash
            if existing is not None:
                if existing.dispute_id != case_id or existing.content_sha256 != digest:
                    raise ConflictError("evidence_id 已存在，但证据内容不一致")
                return {"created": False, "evidence": jsonable(self._evidence_payload(existing))}

            if dispute.state != "SUBMITTED" or dispute.intake_manifest_sha256:
                raise ConflictError("案件材料已经冻结；后续补证必须使用补问或申诉接口")
            evidence = Evidence(
                id=request.evidence_id,
                dispute_id=case_id,
                submitted_by=request.submitted_by,
                recorded_by_id=recorder.id if recorder else None,
                evidence_type=request.evidence_type,
                description=request.description,
                content_text=request.text_content,
                source_system=request.source_system,
                source_record_id=request.source_record_id,
                captured_at=request.captured_at,
                submitted_at=utc_now(),
                content_sha256=digest,
                immutable_uri=request.immutable_uri or f"evidence://{case_id}/{request.evidence_id}",
                integrity_status="HASH_VERIFIED" if request.content_sha256 else "UNVERIFIED",
                related_claim_ids_json=sorted(request.related_claim_ids),
                extracted_facts_json=request.extracted_facts,
                handling_flags_json=request.handling_flags,
            )
            session.add(evidence)
            session.commit()
            return {"created": True, "evidence": jsonable(self._evidence_payload(evidence))}

    def freeze_case_materials(
        self,
        case_id: str,
        request: FreezeCaseMaterialsRequest,
    ) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute = session.scalar(
                select(Dispute).where(Dispute.id == case_id).with_for_update()
            )
            if dispute is None:
                raise NotFoundError(f"案件不存在: {case_id}")
            self._require_actor(session, request.actor_id, {"ADMIN", "REVIEWER"})
            if dispute.intake_manifest_sha256:
                return self._freeze_result(session, dispute, idempotent_replay=True)
            if dispute.state != "SUBMITTED":
                raise ConflictError(f"只有 SUBMITTED 案件可以冻结材料，当前状态为 {dispute.state}")
            if dispute.state_version != request.expected_state_version:
                raise ConflictError(
                    f"状态版本冲突: expected={request.expected_state_version}, actual={dispute.state_version}"
                )

            listings = list(
                session.scalars(
                    select(ListingSnapshot).where(
                        ListingSnapshot.transaction_id == dispute.transaction_id,
                        ListingSnapshot.snapshot_type == "DISPUTE_BASELINE",
                    )
                )
            )
            messages = list(
                session.scalars(
                    select(Message)
                    .where(Message.transaction_id == dispute.transaction_id)
                    .order_by(Message.sent_at, Message.id)
                )
            )
            claims = list(
                session.scalars(select(Claim).where(Claim.dispute_id == case_id))
            )
            if len(listings) != 1:
                raise ValidationError("冻结前必须且只能存在一个 DISPUTE_BASELINE 商品快照")
            if not messages:
                raise ValidationError("冻结前必须导入至少一条交易聊天")
            if not claims:
                raise ValidationError("冻结前必须存在至少一个 Claim")
            if any(len(item.content_sha256) != 64 for item in [*listings, *messages]):
                raise ValidationError("商品或聊天快照缺少合法内容哈希")
            initial_evidence = list(
                session.scalars(select(Evidence).where(Evidence.dispute_id == case_id))
            )
            if any(len(item.content_sha256) != 64 for item in initial_evidence):
                raise ValidationError("初始证据缺少合法内容哈希")

            routing_summary = self.routing_service.ensure_case_routed_in_session(
                session,
                dispute,
                actor_id=request.actor_id,
            )
            if not routing_summary["ready_for_investigation"]:
                raise ConflictError("冻结前必须完成人工分类，并确保所有 material Claim 只绑定一个 Skill")
            policy_id = self.routing_service.policy_id_for_summary(routing_summary)
            policy = PolicyService(session).select_for_transaction(
                policy_id=policy_id,
                paid_at=dispute.transaction.paid_at,
                dispute_type=dispute.dispute_type,
                category=dispute.transaction.category,
            )
            dispute.policy_id = policy.policy_id
            dispute.policy_version = policy.version
            dispute.policy_basis_time = dispute.transaction.paid_at

            for message in messages:
                message.snapshot_locked = True
                message.dispute_id = case_id
            self._ensure_baseline_evidence(
                session,
                dispute,
                listing=listings[0],
                messages=messages,
                claims=claims,
            )
            session.flush()
            baseline_evidence_ids = list(
                session.scalars(select(Evidence.id).where(Evidence.dispute_id == case_id))
            )
            manifest = self._build_manifest(
                session,
                dispute,
                baseline_evidence_ids=baseline_evidence_ids,
            )
            frozen_at = utc_now()
            dispute.intake_manifest_json = manifest
            dispute.intake_manifest_sha256 = content_hash(manifest)
            dispute.materials_frozen_at = frozen_at
            session.flush()
            event = self.state_machine.transition(
                session,
                case_id=case_id,
                trigger="BASELINE_CAPTURED",
                actor_type="SYSTEM",
                actor_id=request.actor_id,
                reason_code="GENERIC_INTAKE_MATERIALS_FROZEN",
                expected_state_version=request.expected_state_version,
                metadata={
                    "intake_manifest_sha256": dispute.intake_manifest_sha256,
                    "listing_snapshot_ids": [item.id for item in listings],
                    "message_count": len(messages),
                    "baseline_evidence_ids": sorted(baseline_evidence_ids),
                    "policy_id": dispute.policy_id,
                    "policy_version": dispute.policy_version,
                    "routing_ready": routing_summary["ready_for_investigation"],
                },
            )
            session.commit()
            session.refresh(dispute)
            result = self._freeze_result(session, dispute, idempotent_replay=False)
            result["case_event_id"] = event.id
            return result

    def get_intake(self, case_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            dispute = self._require_dispute(session, case_id)
            transaction = dispute.transaction
            listings = list(
                session.scalars(
                    select(ListingSnapshot).where(ListingSnapshot.transaction_id == transaction.id)
                )
            )
            messages = list(
                session.scalars(
                    select(Message)
                    .where(Message.transaction_id == transaction.id)
                    .order_by(Message.sent_at, Message.id)
                )
            )
            claims = list(
                session.scalars(
                    select(Claim).where(Claim.dispute_id == case_id).order_by(Claim.asserted_at, Claim.id)
                )
            )
            evidence = list(
                session.scalars(
                    select(Evidence).where(Evidence.dispute_id == case_id).order_by(Evidence.submitted_at, Evidence.id)
                )
            )
            integrity_valid = None
            if dispute.intake_manifest_json and dispute.intake_manifest_sha256:
                frozen_ids = [
                    item["evidence_id"]
                    for item in dispute.intake_manifest_json.get("baseline_evidence", [])
                ]
                current = self._build_manifest(session, dispute, baseline_evidence_ids=frozen_ids)
                integrity_valid = content_hash(current) == dispute.intake_manifest_sha256
            return jsonable(
                {
                    "case_id": case_id,
                    "state": dispute.state,
                    "state_version": dispute.state_version,
                    "transaction": self._transaction_payload(transaction),
                    "listing_snapshots": [self._snapshot_payload(item) for item in listings],
                    "messages": [
                        {
                            "message_id": item.id,
                            "sender_role": item.sender_role,
                            "body": item.body,
                            "sent_at": item.sent_at,
                            "content_sha256": item.content_sha256,
                            "snapshot_locked": item.snapshot_locked,
                        }
                        for item in messages
                    ],
                    "claims": [
                        {
                            "claim_id": item.id,
                            "party": item.party,
                            "claim_type": item.claim_type,
                            "issue_type": item.issue_type,
                            "statement": item.statement,
                            "routing_status": item.routing_status,
                            "skill_name": item.skill_name,
                            "skill_version": item.skill_version,
                            "material": item.material,
                        }
                        for item in claims
                    ],
                    "evidence": [self._evidence_payload(item) for item in evidence],
                    "freeze": {
                        "frozen": bool(dispute.intake_manifest_sha256),
                        "materials_frozen_at": dispute.materials_frozen_at,
                        "intake_manifest_sha256": dispute.intake_manifest_sha256,
                        "manifest_integrity_valid": integrity_valid,
                        "manifest": dispute.intake_manifest_json,
                    },
                }
            )

    def _build_manifest(
        self,
        session: Session,
        dispute: Dispute,
        *,
        baseline_evidence_ids: list[str],
    ) -> dict[str, Any]:
        transaction = dispute.transaction
        listings = list(
            session.scalars(
                select(ListingSnapshot)
                .where(ListingSnapshot.transaction_id == transaction.id)
                .order_by(ListingSnapshot.snapshot_type, ListingSnapshot.id)
            )
        )
        messages = list(
            session.scalars(
                select(Message)
                .where(Message.transaction_id == transaction.id)
                .order_by(Message.sent_at, Message.id)
            )
        )
        claims = list(
            session.scalars(
                select(Claim).where(Claim.dispute_id == dispute.id).order_by(Claim.asserted_at, Claim.id)
            )
        )
        evidence = list(
            session.scalars(
                select(Evidence)
                .where(Evidence.dispute_id == dispute.id, Evidence.id.in_(baseline_evidence_ids or ["__none__"]))
                .order_by(Evidence.id)
            )
        )
        return jsonable(
            {
                "schema_version": "1.0.0",
                "case_id": dispute.id,
                "transaction": self._transaction_payload(transaction),
                "listing_snapshots": [
                    {
                        "snapshot_id": item.id,
                        "snapshot_type": item.snapshot_type,
                        "content_sha256": item.content_sha256,
                        "material_sha256": content_hash(
                            {
                                "transaction_id": item.transaction_id,
                                "listing_id": item.listing_id,
                                "snapshot_type": item.snapshot_type,
                                "payload": item.payload_json,
                                "immutable_uri": item.immutable_uri,
                                "captured_at": item.captured_at,
                            }
                        ),
                        "captured_at": item.captured_at,
                    }
                    for item in listings
                ],
                "messages": [
                    {
                        "message_id": item.id,
                        "content_sha256": item.content_sha256,
                        "material_sha256": content_hash(
                            {
                                "transaction_id": item.transaction_id,
                                "dispute_id": item.dispute_id,
                                "sender_id": item.sender_id,
                                "sender_role": item.sender_role,
                                "body": item.body,
                                "sent_at": item.sent_at,
                                "snapshot_locked": item.snapshot_locked,
                            }
                        ),
                        "sent_at": item.sent_at,
                    }
                    for item in messages
                ],
                "claims": [
                    {
                        "claim_id": item.id,
                        "party": item.party,
                        "statement_sha256": content_hash(item.statement),
                        "asserted_at": item.asserted_at,
                        "material": item.material,
                        "responds_to_claim_id": item.responds_to_claim_id,
                    }
                    for item in claims
                ],
                "baseline_evidence": [
                    {
                        "evidence_id": item.id,
                        "content_sha256": item.content_sha256,
                        "material_sha256": content_hash(
                            {
                                "dispute_id": item.dispute_id,
                                "submitted_by": item.submitted_by,
                                "evidence_type": item.evidence_type,
                                "description": item.description,
                                "text_content": item.content_text,
                                "source_system": item.source_system,
                                "source_record_id": item.source_record_id,
                                "captured_at": item.captured_at,
                                "submitted_at": item.submitted_at,
                                "immutable_uri": item.immutable_uri,
                                "integrity_status": item.integrity_status,
                                "related_claim_ids": item.related_claim_ids_json,
                                "extracted_facts": item.extracted_facts_json,
                                "handling_flags": item.handling_flags_json,
                            }
                        ),
                        "captured_at": item.captured_at,
                    }
                    for item in evidence
                ],
                "policy": {
                    "policy_id": dispute.policy_id,
                    "version": dispute.policy_version,
                    "basis_time": dispute.policy_basis_time,
                },
            }
        )

    def _freeze_result(
        self,
        session: Session,
        dispute: Dispute,
        *,
        idempotent_replay: bool,
    ) -> dict[str, Any]:
        manifest = dispute.intake_manifest_json or {}
        frozen_ids = [item["evidence_id"] for item in manifest.get("baseline_evidence", [])]
        current = self._build_manifest(session, dispute, baseline_evidence_ids=frozen_ids)
        return jsonable(
            {
                "case_id": dispute.id,
                "state": dispute.state,
                "state_version": dispute.state_version,
                "materials_frozen_at": dispute.materials_frozen_at,
                "intake_manifest_sha256": dispute.intake_manifest_sha256,
                "manifest_integrity_valid": bool(
                    dispute.intake_manifest_sha256
                    and content_hash(current) == dispute.intake_manifest_sha256
                ),
                "policy": {
                    "policy_id": dispute.policy_id,
                    "version": dispute.policy_version,
                },
                "idempotent_replay": idempotent_replay,
            }
        )

    @staticmethod
    def _ensure_baseline_evidence(
        session: Session,
        dispute: Dispute,
        *,
        listing: ListingSnapshot,
        messages: list[Message],
        claims: list[Claim],
    ) -> None:
        claim_ids = sorted(item.id for item in claims)
        listing_evidence_id = f"ev_intake_listing_{content_hash([dispute.id, listing.id])[:24]}"
        if session.get(Evidence, listing_evidence_id) is None:
            payload = listing.payload_json
            extracted_facts: list[dict[str, Any]] = []
            field_mapping = {
                "memory_gb": "promised_memory_gb",
                "device_serial": "listing_serial",
                "included_items": "promised_items",
                "promised_items": "promised_items",
                "quantity": "promised_quantity",
            }
            for source_field, fact_field in field_mapping.items():
                if source_field in payload:
                    extracted_facts.append({"field": fact_field, "value": payload[source_field]})
            session.add(
                Evidence(
                    id=listing_evidence_id,
                    dispute_id=dispute.id,
                    submitted_by="SYSTEM",
                    evidence_type="LISTING_SNAPSHOT",
                    description="冻结的交易商品基线快照。",
                    content_text=canonical_json(payload),
                    source_system="LISTING",
                    source_record_id=listing.id,
                    captured_at=listing.captured_at,
                    submitted_at=utc_now(),
                    content_sha256=content_hash(
                        {"case_id": dispute.id, "snapshot_sha256": listing.content_sha256}
                    ),
                    immutable_uri=listing.immutable_uri,
                    integrity_status="SOURCE_VERIFIED",
                    related_claim_ids_json=claim_ids,
                    extracted_facts_json=extracted_facts,
                    handling_flags_json=[],
                )
            )

        chat_evidence_id = f"ev_intake_chat_{content_hash([dispute.id, *[item.id for item in messages]])[:24]}"
        if session.get(Evidence, chat_evidence_id) is None:
            chat_payload = [
                {
                    "message_id": item.id,
                    "sender_role": item.sender_role,
                    "body": item.body,
                    "sent_at": item.sent_at,
                    "content_sha256": item.content_sha256,
                }
                for item in messages
            ]
            session.add(
                Evidence(
                    id=chat_evidence_id,
                    dispute_id=dispute.id,
                    submitted_by="SYSTEM",
                    evidence_type="CHAT_SNAPSHOT",
                    description=f"冻结的交易聊天快照，共 {len(messages)} 条消息。",
                    content_text=canonical_json(chat_payload),
                    source_system="CONVERSATION",
                    source_record_id=f"chat_snapshot_{dispute.id}",
                    captured_at=max(item.sent_at for item in messages),
                    submitted_at=utc_now(),
                    content_sha256=content_hash(
                        {
                            "case_id": dispute.id,
                            "message_hashes": [item.content_sha256 for item in messages],
                        }
                    ),
                    immutable_uri=f"snapshot://conversation/{dispute.transaction_id}/{dispute.id}",
                    integrity_status="SOURCE_VERIFIED",
                    related_claim_ids_json=claim_ids,
                    extracted_facts_json=[],
                    handling_flags_json=[],
                )
            )

    @staticmethod
    def _verify_digest(provided: str | None, computed: str, label: str) -> None:
        if provided is not None and provided.casefold() != computed:
            raise ValidationError(f"{label} content_sha256 与规范化内容不一致")

    @staticmethod
    def _require_actor(session: Session, actor_id: str, allowed_roles: set[str]) -> User:
        actor = session.get(User, actor_id)
        if actor is None or actor.role not in allowed_roles:
            raise AuthorizationError(
                f"actor_id={actor_id} 必须是以下角色之一：{', '.join(sorted(allowed_roles))}"
            )
        return actor

    @staticmethod
    def _require_transaction(session: Session, transaction_id: str) -> Transaction:
        transaction = session.get(Transaction, transaction_id)
        if transaction is None:
            raise NotFoundError(f"交易不存在: {transaction_id}")
        return transaction

    @staticmethod
    def _require_dispute(session: Session, case_id: str) -> Dispute:
        dispute = session.get(Dispute, case_id)
        if dispute is None:
            raise NotFoundError(f"案件不存在: {case_id}")
        return dispute

    @staticmethod
    def _require_transaction_materials_mutable(session: Session, transaction_id: str) -> None:
        disputes = list(session.scalars(select(Dispute).where(Dispute.transaction_id == transaction_id)))
        frozen = [
            item.id
            for item in disputes
            if item.intake_manifest_sha256 or item.state != "SUBMITTED"
        ]
        if frozen:
            raise ConflictError(f"交易材料已被案件冻结，禁止修改基线: {', '.join(frozen)}")

    @staticmethod
    def _transaction_payload(item: Transaction) -> dict[str, Any]:
        return {
            "transaction_id": item.id,
            "buyer_id": item.buyer_id,
            "seller_id": item.seller_id,
            "listing_id": item.listing_id,
            "category": item.category,
            "paid_amount_minor": item.paid_amount_minor,
            "currency": item.currency,
            "paid_at": item.paid_at,
            "delivered_at": item.delivered_at,
            "order_status": item.order_status,
            "funds_status": item.funds_status,
        }

    @staticmethod
    def _snapshot_payload(item: ListingSnapshot) -> dict[str, Any]:
        return {
            "snapshot_id": item.id,
            "transaction_id": item.transaction_id,
            "listing_id": item.listing_id,
            "snapshot_type": item.snapshot_type,
            "payload": item.payload_json,
            "captured_at": item.captured_at,
            "content_sha256": item.content_sha256,
            "immutable_uri": item.immutable_uri,
        }

    @staticmethod
    def _evidence_payload(item: Evidence) -> dict[str, Any]:
        return {
            "evidence_id": item.id,
            "case_id": item.dispute_id,
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
            "immutable_uri": item.immutable_uri,
            "integrity_status": item.integrity_status,
            "related_claim_ids": item.related_claim_ids_json,
            "extracted_facts": item.extracted_facts_json,
            "handling_flags": item.handling_flags_json,
        }

    @staticmethod
    def _dispute_matches_request(
        session: Session,
        dispute: Dispute,
        request: DisputeSubmitRequest,
    ) -> bool:
        if (
            dispute.transaction_id != request.transaction_id
            or dispute.dispute_type != request.dispute_type.value
        ):
            return False
        claims = list(
            session.scalars(
                select(Claim).where(Claim.dispute_id == dispute.id).order_by(Claim.id)
            )
        )
        expected = sorted(request.claims, key=lambda item: item.claim_id)
        if len(claims) != len(expected):
            return False
        return all(
            actual.id == wanted.claim_id
            and actual.party == wanted.party
            and actual.claim_type == wanted.claim_type
            and actual.statement == wanted.statement
            and actual.asserted_at == wanted.asserted_at
            and actual.material == wanted.material
            and actual.responds_to_claim_id == wanted.responds_to_claim_id
            for actual, wanted in zip(claims, expected)
        )
