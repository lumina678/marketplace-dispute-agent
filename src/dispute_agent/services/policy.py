from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from dispute_agent.errors import NotFoundError, PolicySelectionError
from dispute_agent.models import PolicyVersion


class PolicyService:
    def __init__(self, session: Session):
        self.session = session

    def select_for_transaction(
        self,
        *,
        policy_id: str,
        paid_at: datetime,
        dispute_type: str = "DESCRIPTION_MISMATCH",
        category: str = "USED_LAPTOP",
    ) -> PolicyVersion:
        candidates = list(
            self.session.scalars(
                select(PolicyVersion).where(
                    PolicyVersion.policy_id == policy_id,
                    PolicyVersion.status != "DRAFT",
                    PolicyVersion.effective_from <= paid_at,
                    or_(PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to > paid_at),
                )
            )
        )
        scoped = [
            item
            for item in candidates
            if dispute_type in item.document_json["scope"]["dispute_types"]
            and category in item.document_json["scope"]["listing_categories"]
        ]
        if len(scoped) != 1:
            raise PolicySelectionError(
                f"{policy_id} 在 paid_at={paid_at.isoformat()} 应唯一匹配，实际匹配 {len(scoped)} 个版本"
            )
        return scoped[0]

    def get_version(self, policy_id: str, version: str) -> PolicyVersion:
        policy = self.session.scalar(
            select(PolicyVersion).where(
                PolicyVersion.policy_id == policy_id,
                PolicyVersion.version == version,
            )
        )
        if policy is None:
            raise NotFoundError(f"政策不存在: {policy_id}@{version}")
        return policy

    def search_rules(self, policy: PolicyVersion, query: str, limit: int = 10) -> list[dict]:
        normalized = query.strip().casefold()
        rules = policy.document_json["rules"]
        if not normalized:
            matches = rules
        else:
            matches = [
                rule
                for rule in rules
                if normalized in f"{rule['rule_id']} {rule['title']} {rule['text']}".casefold()
            ]
        return matches[:limit]
