from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dispute_agent.dispute_types import DisputeType


ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"
SHA256_PATTERN = r"^[0-9a-fA-F]{64}$"


class IntakeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def datetimes_require_timezone(cls, value):  # type: ignore[no-untyped-def]
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("时间字段必须包含时区")
        return value


class TransactionCreateRequest(IntakeModel):
    transaction_id: str = Field(pattern=ID_PATTERN)
    buyer_id: str = Field(pattern=ID_PATTERN)
    seller_id: str = Field(pattern=ID_PATTERN)
    listing_id: str = Field(pattern=ID_PATTERN)
    category: str = Field(default="USED_LAPTOP", pattern=r"^[A-Z][A-Z0-9_]+$")
    paid_amount_minor: int = Field(ge=0)
    currency: Literal["CNY"] = "CNY"
    paid_at: datetime
    delivered_at: datetime | None = None
    order_status: Literal["PAID", "SHIPPED", "DELIVERED", "DISPUTED"] = "PAID"
    funds_status: Literal["HELD", "RELEASED", "REFUNDED", "PARTIALLY_REFUNDED"] = "HELD"
    actor_id: str = Field(default="user_admin_demo", pattern=ID_PATTERN)

    @model_validator(mode="after")
    def validate_parties_and_timeline(self) -> "TransactionCreateRequest":
        if self.buyer_id == self.seller_id:
            raise ValueError("buyer_id 和 seller_id 不能相同")
        if self.delivered_at is not None and self.delivered_at < self.paid_at:
            raise ValueError("delivered_at 不能早于 paid_at")
        return self


class ListingSnapshotImportRequest(IntakeModel):
    snapshot_id: str = Field(pattern=ID_PATTERN)
    snapshot_type: Literal["DISPUTE_BASELINE"] = "DISPUTE_BASELINE"
    payload: dict[str, Any] = Field(min_length=1)
    captured_at: datetime
    content_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    immutable_uri: str | None = Field(default=None, max_length=255)
    actor_id: str = Field(default="user_admin_demo", pattern=ID_PATTERN)


class ChatMessageImport(IntakeModel):
    message_id: str = Field(pattern=ID_PATTERN)
    sender_role: Literal["BUYER", "SELLER"]
    body: str = Field(min_length=1, max_length=20_000)
    sent_at: datetime
    content_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)


class ChatBatchImportRequest(IntakeModel):
    messages: list[ChatMessageImport] = Field(min_length=1, max_length=500)
    actor_id: str = Field(default="user_admin_demo", pattern=ID_PATTERN)

    @model_validator(mode="after")
    def message_ids_are_unique(self) -> "ChatBatchImportRequest":
        ids = [item.message_id for item in self.messages]
        if len(ids) != len(set(ids)):
            raise ValueError("同一批次 message_id 不能重复")
        return self


class ClaimSubmitInput(IntakeModel):
    claim_id: str = Field(pattern=ID_PATTERN)
    party: Literal["BUYER", "SELLER"]
    claim_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    statement: str = Field(min_length=1, max_length=10_000)
    asserted_at: datetime
    material: bool = True
    responds_to_claim_id: str | None = Field(default=None, pattern=ID_PATTERN)


class DisputeSubmitRequest(IntakeModel):
    case_id: str = Field(pattern=ID_PATTERN)
    transaction_id: str = Field(pattern=ID_PATTERN)
    dispute_type: DisputeType
    submitted_by_id: str = Field(pattern=ID_PATTERN)
    claims: list[ClaimSubmitInput] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def claim_ids_are_unique(self) -> "DisputeSubmitRequest":
        ids = [item.claim_id for item in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claims.claim_id 不能重复")
        known = set(ids)
        if any(item.responds_to_claim_id and item.responds_to_claim_id not in known for item in self.claims):
            raise ValueError("responds_to_claim_id 必须引用本次提交中的 Claim")
        return self


class TextEvidenceUploadRequest(IntakeModel):
    evidence_id: str = Field(pattern=ID_PATTERN)
    submitted_by: Literal["BUYER", "SELLER", "SYSTEM", "REVIEWER", "THIRD_PARTY"]
    submitter_id: str = Field(pattern=ID_PATTERN)
    evidence_type: Literal[
        "DOCUMENT",
        "CHAT_SNAPSHOT",
        "PARTY_STATEMENT",
        "DEVICE_REPORT",
        "SHIPMENT_EVENT",
    ]
    description: str = Field(min_length=1, max_length=2_000)
    text_content: str = Field(min_length=1, max_length=100_000)
    source_record_id: str = Field(pattern=ID_PATTERN)
    captured_at: datetime
    related_claim_ids: list[str] = Field(min_length=1, max_length=50)
    extracted_facts: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    content_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    immutable_uri: str | None = Field(default=None, max_length=255)
    source_system: str = Field(default="INTAKE_TEXT_EVIDENCE", pattern=r"^[A-Z][A-Z0-9_]+$")
    handling_flags: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("related_claim_ids")
    @classmethod
    def related_claim_ids_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("related_claim_ids 不能重复")
        return value


class FreezeCaseMaterialsRequest(IntakeModel):
    actor_id: str = Field(default="user_admin_demo", pattern=ID_PATTERN)
    expected_state_version: int = Field(ge=1)
