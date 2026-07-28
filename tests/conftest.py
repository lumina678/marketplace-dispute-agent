from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.config import PROJECT_ROOT, Settings, get_settings
from dispute_agent.db import Base, create_database_engine
from dispute_agent.seed import seed_database
from dispute_agent.services.orchestrator import CaseOrchestrator
from dispute_agent.services.state_machine import StateMachineService
from dispute_agent.services.tools import ToolService


@dataclass
class TestContext:
    engine: Engine
    sessions: sessionmaker[Session]
    settings: Settings
    tools: ToolService
    orchestrator: CaseOrchestrator
    state_machine: StateMachineService


@pytest.fixture
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestContext:
    # Tests must remain deterministic even when a developer has configured a
    # private model in the project-level .env for interactive workbench use.
    monkeypatch.setenv("XIANYU_MODEL_BACKEND", "rule_based")
    monkeypatch.setenv("XIANYU_WORKFLOW_QUEUE_BACKEND", "inline")
    get_settings.cache_clear()
    database_url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_database_engine(database_url)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, class_=Session, autoflush=False, expire_on_commit=False)
    with sessions() as session:
        seed_database(session, PROJECT_ROOT / "policies")
    settings = Settings(
        database_url=database_url,
        policy_directory=PROJECT_ROOT / "policies",
        state_machine_path=PROJECT_ROOT / "config" / "case_state_machine.json",
        default_tool_call_budget=30,
        default_token_budget=40000,
        max_phase_failures=2,
        workflow_queue_backend="inline",
        workflow_job_timeout_seconds=120,
        workflow_heartbeat_interval_seconds=1,
        workflow_stale_after_seconds=5,
    )
    state_machine = StateMachineService(settings.state_machine_path)
    result = TestContext(
        engine=engine,
        sessions=sessions,
        settings=settings,
        tools=ToolService(sessions),
        orchestrator=CaseOrchestrator(sessions, settings=settings, state_machine=state_machine),
        state_machine=state_machine,
    )
    yield result
    engine.dispose()
    get_settings.cache_clear()
