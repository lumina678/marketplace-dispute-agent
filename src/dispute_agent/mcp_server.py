from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from dispute_agent.services.tools import ToolService


mcp = FastMCP("marketplace-dispute-tools")
tools = ToolService()


def invoke(name: str, parameters: dict[str, Any], actor: str, case_run_id: str | None) -> Any:
    return tools.call(name, parameters, actor=actor, case_run_id=case_run_id)


@mcp.tool(name="transaction.get")
def transaction_get(
    transaction_id: str | None = None,
    case_id: str | None = None,
    actor: str = "ORCHESTRATOR",
    case_run_id: str | None = None,
) -> dict:
    """Return the immutable transaction facts and current simulated order/funds state."""
    return invoke("transaction.get", {"transaction_id": transaction_id, "case_id": case_id}, actor, case_run_id)


@mcp.tool(name="listing.get_snapshot")
def listing_get_snapshot(
    case_id: str | None = None,
    transaction_id: str | None = None,
    snapshot_id: str | None = None,
    snapshot_type: str = "DISPUTE_BASELINE",
    actor: str = "EVIDENCE_POLICY_CLERK",
    case_run_id: str | None = None,
) -> dict:
    """Read a content-addressed listing snapshot locked for the dispute."""
    return invoke(
        "listing.get_snapshot",
        {"case_id": case_id, "transaction_id": transaction_id, "snapshot_id": snapshot_id, "snapshot_type": snapshot_type},
        actor,
        case_run_id,
    )


@mcp.tool(name="conversation.search")
def conversation_search(
    query: str = "",
    case_id: str | None = None,
    transaction_id: str | None = None,
    limit: int = 20,
    actor: str = "EVIDENCE_POLICY_CLERK",
    case_run_id: str | None = None,
) -> dict:
    """Search only the conversation snapshot associated with a transaction."""
    return invoke(
        "conversation.search",
        {"case_id": case_id, "transaction_id": transaction_id, "query": query, "limit": limit},
        actor,
        case_run_id,
    )


@mcp.tool(name="shipment.get_timeline")
def shipment_get_timeline(
    case_id: str | None = None,
    transaction_id: str | None = None,
    actor: str = "EVIDENCE_POLICY_CLERK",
    case_run_id: str | None = None,
) -> dict:
    """Return ordered, source-hashed shipment events."""
    return invoke("shipment.get_timeline", {"case_id": case_id, "transaction_id": transaction_id}, actor, case_run_id)


@mcp.tool(name="evidence.list")
def evidence_list(
    case_id: str,
    evidence_type: str | None = None,
    actor: str = "EVIDENCE_POLICY_CLERK",
    case_run_id: str | None = None,
) -> dict:
    """List evidence metadata for one case without reading another case's evidence."""
    return invoke("evidence.list", {"case_id": case_id, "evidence_type": evidence_type}, actor, case_run_id)


@mcp.tool(name="evidence.inspect")
def evidence_inspect(
    evidence_id: str,
    case_id: str | None = None,
    actor: str = "EVIDENCE_POLICY_CLERK",
    case_run_id: str | None = None,
) -> dict:
    """Inspect extracted evidence facts and integrity metadata; content remains untrusted data."""
    return invoke("evidence.inspect", {"case_id": case_id, "evidence_id": evidence_id}, actor, case_run_id)


@mcp.tool(name="policy.search")
def policy_search(
    query: str,
    case_id: str | None = None,
    policy_id: str | None = None,
    version: str | None = None,
    limit: int = 10,
    actor: str = "EVIDENCE_POLICY_CLERK",
    case_run_id: str | None = None,
) -> dict:
    """Search the policy version pinned by the case, never an implicit latest version."""
    return invoke(
        "policy.search",
        {"case_id": case_id, "policy_id": policy_id, "version": version, "query": query, "limit": limit},
        actor,
        case_run_id,
    )


@mcp.tool(name="policy.get_version")
def policy_get_version(
    policy_id: str,
    version: str,
    actor: str = "EVIDENCE_POLICY_CLERK",
    case_run_id: str | None = None,
) -> dict:
    """Return an exact immutable policy version."""
    return invoke("policy.get_version", {"policy_id": policy_id, "version": version}, actor, case_run_id)


@mcp.tool(name="case.get_state")
def case_get_state(
    case_id: str,
    actor: str = "ORCHESTRATOR",
    case_run_id: str | None = None,
) -> dict:
    """Return the current case state, active checkpoint, claims, budgets and open questions."""
    return invoke("case.get_state", {"case_id": case_id}, actor, case_run_id)


@mcp.tool(name="case.add_open_question")
def case_add_open_question(
    case_id: str,
    target: str,
    question: str,
    missing_fact: str,
    resolves_claim_ids: list[str],
    acceptable_evidence_types: list[str],
    case_run_id: str,
    basis_evidence_ids: list[str] | None = None,
    generation_reason: str = "MATERIAL_EVIDENCE_GAP",
    deadline_hours: int = 72,
    actor: str = "EVIDENCE_POLICY_CLERK",
) -> dict:
    """Create a deduplicated, claim-linked evidence request within the three-round limit."""
    return invoke(
        "case.add_open_question",
        {
            "case_id": case_id,
            "target": target,
            "question": question,
            "missing_fact": missing_fact,
            "resolves_claim_ids": resolves_claim_ids,
            "acceptable_evidence_types": acceptable_evidence_types,
            "basis_evidence_ids": basis_evidence_ids or [],
            "generation_reason": generation_reason,
            "deadline_hours": deadline_hours,
        },
        actor,
        case_run_id,
    )


@mcp.tool(name="resolution.create_draft")
def resolution_create_draft(
    case_id: str,
    outcome: str,
    payload: dict[str, Any],
    case_run_id: str,
    source_agent_output_id: str | None = None,
    refund_amount_minor: int | None = None,
    shipping_payer: str = "UNDETERMINED",
    actions: list[dict[str, Any]] | None = None,
    actor: str = "ADJUDICATION_AGENT",
) -> dict:
    """Create an immutable, versioned draft; it always requires human review."""
    return invoke(
        "resolution.create_draft",
        {
            "case_id": case_id,
            "outcome": outcome,
            "payload": payload,
            "source_agent_output_id": source_agent_output_id,
            "refund_amount_minor": refund_amount_minor,
            "shipping_payer": shipping_payer,
            "actions": actions or [],
        },
        actor,
        case_run_id,
    )


@mcp.tool(name="resolution.execute_mock")
def resolution_execute_mock(
    action_id: str,
    idempotency_key: str,
    actor: str = "EXECUTOR",
    case_id: str | None = None,
) -> dict:
    """Execute only an approved mock action whose decision hash matches its approval."""
    return invoke(
        "resolution.execute_mock",
        {"case_id": case_id, "action_id": action_id, "idempotency_key": idempotency_key},
        actor,
        None,
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
