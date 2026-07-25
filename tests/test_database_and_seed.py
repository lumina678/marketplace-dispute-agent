from __future__ import annotations

from sqlalchemy import func, inspect, select

from dispute_agent.models import Claim, Dispute, PolicyVersion, Transaction, User
from dispute_agent.seed import seed_database


def test_schema_and_seed_are_complete_and_idempotent(context) -> None:
    with context.sessions() as session:
        counts = {
            "users": session.scalar(select(func.count()).select_from(User)),
            "transactions": session.scalar(select(func.count()).select_from(Transaction)),
            "disputes": session.scalar(select(func.count()).select_from(Dispute)),
            "policy_versions": session.scalar(select(func.count()).select_from(PolicyVersion)),
        }
        assert counts == {"users": 4, "transactions": 5, "disputes": 5, "policy_versions": 2}
        second = seed_database(session, context.settings.policy_directory)
        assert second["disputes"] == 5
        assert second["policy_versions"] == 2

        claims = list(session.scalars(select(Claim).order_by(Claim.id)))
        assert claims
        assert all(item.issue_type == "DESCRIPTION_MISMATCH" for item in claims)
        assert all(item.routing_status == "ROUTED" for item in claims)
        assert all((item.skill_name, item.skill_version) == ("description-mismatch", "1.0.0") for item in claims)

    table_names = set(inspect(context.engine).get_table_names())
    assert {
        "users",
        "transactions",
        "listing_snapshots",
        "disputes",
        "claims",
        "messages",
        "evidence",
        "shipment_events",
        "policy_versions",
        "decisions",
        "appeals",
        "resolution_actions",
        "case_runs",
        "tool_calls",
        "approvals",
        "case_events",
        "open_questions",
    }.issubset(table_names)


def test_seeded_appeal_history_can_be_replayed(context) -> None:
    replay = context.orchestrator.replay("case_appeal_reversal")
    assert replay == {
        "case_id": "case_appeal_reversal",
        "state": "APPEALED",
        "state_version": 8,
        "event_count": 7,
        "consistent": True,
    }
