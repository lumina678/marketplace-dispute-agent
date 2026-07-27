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


def case_routing_summary(claims: list[Claim]) -> dict[str, Any]:
    material = [item for item in claims if item.material]
    resolved_statuses = {"ROUTED", "OVERRIDDEN"}
    unresolved = [
        item.id
        for item in material
        if item.routing_status not in resolved_statuses or not item.skill_name or not item.skill_version
    ]
    issue_types = sorted({item.issue_type for item in material if item.issue_type})
    skill_bindings = sorted(
        {
            f"{item.skill_name}@{item.skill_version}"
            for item in material
            if item.skill_name and item.skill_version
        }
    )
    compound = len(issue_types) > 1 or len(skill_bindings) > 1
    return {
        "material_claim_count": len(material),
        "primary_issue_type": issue_types[0] if len(issue_types) == 1 else None,
        "issue_types": issue_types,
        "skill_bindings": skill_bindings,
        "unresolved_claim_ids": unresolved,
        "compound": compound,
        "ready_for_investigation": bool(material) and not unresolved and not compound and len(skill_bindings) == 1,
    }
