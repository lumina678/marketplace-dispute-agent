from __future__ import annotations

from typing import Any

from dispute_agent.models import Claim


def claim_routing_payload(claim: Claim) -> dict[str, Any]:
    """Canonical persisted routing view shared by tools, UI and checkpoints."""

    return {
        "issue_type": claim.issue_type,
        "issue_subtype": claim.issue_subtype,
        "routing_status": claim.routing_status,
        "routing_source": claim.routing_source,
        "routing_reason": claim.routing_reason,
        "routing_confidence": claim.routing_confidence,
        "skill_name": claim.skill_name,
        "skill_version": claim.skill_version,
    }

