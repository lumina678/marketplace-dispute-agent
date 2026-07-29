from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fakeredis import FakeRedis
from pydantic import ValidationError
from pwdlib import PasswordHash
from sqlalchemy import select

from dispute_agent.api import create_app
from dispute_agent.auth import (
    InMemorySessionStore,
    RedisSessionStore,
    ReviewerSession,
    SessionStoreUnavailable,
)
from dispute_agent.config import Settings
from dispute_agent.models import Approval, Evidence, ToolCall, User, WorkflowJob
from dispute_agent.reviewer_cli import upsert_reviewer


PASSWORD = "reviewer-test-password-2026"


def auth_app(context, *, secure_cookie: bool = False):  # type: ignore[no-untyped-def]
    settings = context.settings.model_copy(
        update={
            "auth_enabled": True,
            "auth_cookie_secure": secure_cookie,
            "auth_session_ttl_seconds": 3600,
        }
    )
    store = InMemorySessionStore()
    app = create_app(context.sessions, settings=settings, auth_session_store=store)
    return app, store, settings


def create_reviewer(context, *, user_id: str = "reviewer_auth", username: str = "reviewer.auth") -> User:  # type: ignore[no-untyped-def]
    return upsert_reviewer(
        username=username,
        display_name="认证测试审核员",
        password=PASSWORD,
        user_id=user_id,
        session_factory=context.sessions,
    )


async def login(client: httpx.AsyncClient, username: str = "reviewer.auth") -> dict:
    response = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    me = await client.get("/auth/me")
    assert me.status_code == 200, me.text
    return me.json()


def test_reviewer_login_me_cookie_and_generic_failures(context) -> None:  # type: ignore[no-untyped-def]
    create_reviewer(context)
    with context.sessions() as session:
        seller = session.get(User, "user_seller_demo")
        assert seller is not None
        seller.username = "seller.login"
        seller.password_hash = PasswordHash.recommended().hash(PASSWORD)
        session.commit()

    async def exercise() -> None:
        app, _store, settings = auth_app(context)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/cases")).status_code == 401
            assert (await client.get("/openapi.json")).status_code == 401

            wrong = await client.post(
                "/auth/login",
                json={"username": "reviewer.auth", "password": "incorrect-password"},
            )
            non_reviewer = await client.post(
                "/auth/login",
                json={"username": "seller.login", "password": PASSWORD},
            )
            assert wrong.status_code == non_reviewer.status_code == 401
            assert wrong.json()["detail"] == non_reviewer.json()["detail"] == "用户名或密码错误"

            response = await client.post(
                "/auth/login",
                json={"username": "Reviewer.Auth", "password": PASSWORD},
            )
            assert response.status_code == 200
            cookie = response.headers["set-cookie"]
            assert settings.auth_session_cookie_name in cookie
            assert "HttpOnly" in cookie
            assert "SameSite=strict" in cookie
            assert "Secure" not in cookie
            assert "session_id" not in response.text
            assert response.headers["x-frame-options"] == "DENY"
            assert response.headers["x-content-type-options"] == "nosniff"

            me = await client.get("/auth/me")
            assert me.json()["reviewer"] == {
                "id": "reviewer_auth",
                "username": "reviewer.auth",
                "display_name": "认证测试审核员",
                "role": "REVIEWER",
            }
            assert me.json()["csrf_token"]
            assert (await client.get("/cases")).status_code == 200

    asyncio.run(exercise())


def test_csrf_trusted_identity_workflow_approval_and_sse(context) -> None:  # type: ignore[no-untyped-def]
    create_reviewer(context)

    async def exercise() -> None:
        app, _store, _settings = auth_app(context)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            me = await login(client)
            csrf = me["csrf_token"]

            missing = await client.post("/cases/case_clear_mismatch/workflow")
            wrong = await client.post(
                "/cases/case_clear_mismatch/workflow",
                headers={"X-CSRF-Token": "wrong"},
            )
            assert missing.status_code == wrong.status_code == 403

            tool = await client.post(
                "/tools/case.get_state",
                headers={"X-CSRF-Token": csrf},
                json={
                    "actor": "ADMIN",
                    "parameters": {"case_id": "case_clear_mismatch"},
                },
            )
            assert tool.status_code == 200

            evidence_response = await client.post(
                "/cases/case_seller_preshipment/evidence",
                headers={"X-CSRF-Token": csrf},
                json={
                    "evidence_id": "ev_auth_recorded_party_statement",
                    "submitted_by": "BUYER",
                    "submitter_id": "user_buyer_demo",
                    "evidence_type": "PARTY_STATEMENT",
                    "description": "审核员代录的买方文字陈述",
                    "text_content": "买方称设备签收后检测内存为 8GB。",
                    "source_record_id": "auth_recorded_party_statement",
                    "captured_at": "2026-07-14T10:00:00+08:00",
                    "related_claim_ids": ["claim_seller_preshipment_buyer"],
                },
            )
            assert evidence_response.status_code == 200, evidence_response.text
            assert evidence_response.json()["evidence"]["submitted_by"] == "BUYER"
            assert evidence_response.json()["evidence"]["recorded_by_id"] == "reviewer_auth"

            workflow = await client.post(
                "/cases/case_clear_mismatch/workflow",
                headers={"X-CSRF-Token": csrf},
                json={"actor_id": "forged-reviewer"},
            )
            assert workflow.status_code == 202, workflow.text
            job = workflow.json()
            assert job["actor_id"] == "reviewer_auth"

            review_package = await client.get("/cases/case_clear_mismatch/review-package")
            decision_id = review_package.json()["decision"]["decision_id"]
            approval = await client.post(
                "/cases/case_clear_mismatch/reviews/approve",
                headers={"X-CSRF-Token": csrf},
                json={
                    "decision_id": decision_id,
                    "reviewer_id": "user_admin_demo",
                    "reason": "已复核规则、证据和金额。",
                },
            )
            assert approval.status_code == 200, approval.text

            stream = await client.get(f"/workflow-jobs/{job['job_id']}/events")
            assert stream.status_code == 200
            assert "JOB_COMPLETED" in stream.text

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as anonymous:
            assert (await anonymous.get(f"/workflow-jobs/{job['job_id']}/events")).status_code == 401

        with context.sessions() as session:
            stored_job = session.get(WorkflowJob, job["job_id"])
            stored_approval = session.scalar(select(Approval).where(Approval.decision_id == decision_id))
            stored_tool_call = session.scalar(
                select(ToolCall)
                .where(
                    ToolCall.tool_name == "case.get_state",
                    ToolCall.actor_id == "reviewer_auth",
                )
                .order_by(ToolCall.called_at.desc())
            )
            recorded_evidence = session.get(Evidence, "ev_auth_recorded_party_statement")
            assert stored_job is not None and stored_job.actor_id == "reviewer_auth"
            assert stored_approval is not None and stored_approval.reviewer_id == "reviewer_auth"
            assert stored_tool_call is not None
            assert stored_tool_call.actor == "REVIEWER"
            assert stored_tool_call.actor_id == "reviewer_auth"
            assert recorded_evidence is not None
            assert recorded_evidence.submitted_by == "BUYER"
            assert recorded_evidence.recorded_by_id == "reviewer_auth"

    asyncio.run(exercise())


def test_logout_and_expired_session_are_rejected(context) -> None:  # type: ignore[no-untyped-def]
    create_reviewer(context)

    async def exercise() -> None:
        app, store, settings = auth_app(context)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            me = await login(client)
            token = client.cookies.get(settings.auth_session_cookie_name)
            assert token
            store.expire_now(token)
            assert (await client.get("/cases")).status_code == 401

            me = await login(client)
            with context.sessions() as session:
                reviewer = session.get(User, "reviewer_auth")
                assert reviewer is not None
                reviewer.is_active = False
                session.commit()
            assert (await client.get("/cases")).status_code == 401
            with context.sessions() as session:
                reviewer = session.get(User, "reviewer_auth")
                assert reviewer is not None
                reviewer.is_active = True
                session.commit()

            me = await login(client)
            logout = await client.post("/auth/logout", headers={"X-CSRF-Token": me["csrf_token"]})
            assert logout.status_code == 200
            assert (await client.get("/cases")).status_code == 401

    asyncio.run(exercise())


def test_secure_cookie_profile_and_redis_session_ttl(context) -> None:  # type: ignore[no-untyped-def]
    create_reviewer(context)

    async def exercise_secure_cookie() -> None:
        app, _store, _settings = auth_app(context, secure_cookie=True)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
            response = await client.post(
                "/auth/login",
                json={"username": "reviewer.auth", "password": PASSWORD},
            )
            assert response.status_code == 200
            assert "Secure" in response.headers["set-cookie"]

    asyncio.run(exercise_secure_cookie())

    settings = context.settings.model_copy(
        update={"auth_enabled": True, "auth_session_ttl_seconds": 600}
    )
    store = RedisSessionStore(settings)
    store.redis = FakeRedis(decode_responses=True)
    now = datetime.now(timezone.utc)
    session = ReviewerSession(
        session_id="opaque-browser-token",
        reviewer_id="reviewer_auth",
        username="reviewer.auth",
        display_name="认证测试审核员",
        role="REVIEWER",
        csrf_token="csrf-token",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=10)).isoformat(),
    )
    store.create(session, 600)
    assert 0 < store.ttl(session.session_id) <= 600
    assert store.get(session.session_id) == session
    keys = [item.decode() if isinstance(item, bytes) else item for item in store.redis.keys("*")]
    assert all("opaque-browser-token" not in item for item in keys)
    store.delete(session.session_id)
    assert store.get(session.session_id) is None


def test_reviewer_cli_upserts_existing_account(context) -> None:  # type: ignore[no-untyped-def]
    first = create_reviewer(context)
    second = upsert_reviewer(
        username="reviewer.auth",
        display_name="更新后的审核员",
        password="updated-reviewer-password-2026",
        session_factory=context.sessions,
    )
    assert second.id == first.id
    assert second.display_name == "更新后的审核员"
    assert second.password_hash and second.password_hash.startswith("$argon2")


def test_public_deployment_cannot_disable_auth_or_secure_cookie() -> None:
    base = {
        "_env_file": None,
        "environment": "production",
        "database_url": "postgresql+psycopg://user:password@postgres/xianyu",
        "workflow_queue_backend": "rq",
        "public_base_url": "https://disputes.example.com",
    }
    with pytest.raises(ValidationError, match="must enable reviewer authentication"):
        Settings(**base, auth_enabled=False)
    with pytest.raises(ValidationError, match="must use Secure reviewer session cookies"):
        Settings(**base, auth_cookie_secure=False)


def test_session_store_outage_returns_service_unavailable(context) -> None:  # type: ignore[no-untyped-def]
    create_reviewer(context)

    class BrokenStore(InMemorySessionStore):
        def create(self, session: ReviewerSession, ttl_seconds: int) -> None:
            raise SessionStoreUnavailable("test session outage")

    async def exercise() -> None:
        settings = context.settings.model_copy(update={"auth_enabled": True})
        app = create_app(context.sessions, settings=settings, auth_session_store=BrokenStore())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/auth/login",
                json={"username": "reviewer.auth", "password": PASSWORD},
            )
            assert response.status_code == 503
            assert response.json()["detail"] == "test session outage"

    asyncio.run(exercise())
