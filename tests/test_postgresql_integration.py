from __future__ import annotations

import os
from datetime import timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.db import create_database_engine
from dispute_agent.models import User, utc_now


@pytest.mark.skipif(
    not os.getenv("XIANYU_TEST_POSTGRES_URL"),
    reason="set XIANYU_TEST_POSTGRES_URL to run the PostgreSQL integration check",
)
def test_postgresql_uses_native_timestamptz_and_round_trips_aware_values() -> None:
    engine = create_database_engine(os.environ["XIANYU_TEST_POSTGRES_URL"])
    sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    try:
        columns = {item["name"]: item for item in inspect(engine).get_columns("users")}
        assert columns["created_at"]["type"].timezone is True

        now = utc_now()
        with sessions.begin() as session:
            session.merge(
                User(
                    id="postgres_integration_probe",
                    role="ADMIN",
                    display_name="PostgreSQL integration probe",
                    simulated_balance_minor=0,
                    created_at=now,
                )
            )
        with sessions() as session:
            loaded = session.scalar(select(User).where(User.id == "postgres_integration_probe"))
            assert loaded is not None
            assert loaded.created_at.tzinfo is not None
            assert loaded.created_at.astimezone(timezone.utc) == now.astimezone(timezone.utc)
            session.delete(loaded)
            session.commit()
    finally:
        engine.dispose()
