from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Protocol

from pwdlib import PasswordHash
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dispute_agent.config import Settings
from dispute_agent.db import SessionLocal
from dispute_agent.models import User, utc_now


PASSWORD_HASH = PasswordHash.recommended()
DUMMY_PASSWORD_HASH = PASSWORD_HASH.hash("xianyu-dummy-password-never-used")


class SessionStoreUnavailable(RuntimeError):
    """Raised when the server-side session backend cannot be reached."""


@dataclass(frozen=True)
class ReviewerSession:
    session_id: str
    reviewer_id: str
    username: str
    display_name: str
    role: str
    csrf_token: str
    created_at: str
    expires_at: str

    @classmethod
    def from_json(cls, value: str) -> "ReviewerSession":
        return cls(**json.loads(value))

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


class SessionStore(Protocol):
    def create(self, session: ReviewerSession, ttl_seconds: int) -> None: ...

    def get(self, session_id: str) -> ReviewerSession | None: ...

    def delete(self, session_id: str) -> None: ...

    def ttl(self, session_id: str) -> int: ...


def _session_key(prefix: str, session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"


class RedisSessionStore:
    def __init__(self, settings: Settings):
        self.prefix = settings.auth_session_redis_prefix
        self.redis = Redis.from_url(settings.redis_url, decode_responses=True)

    def create(self, session: ReviewerSession, ttl_seconds: int) -> None:
        try:
            self.redis.setex(_session_key(self.prefix, session.session_id), ttl_seconds, session.to_json())
        except RedisError as exc:
            raise SessionStoreUnavailable("审核员 Session 服务暂时不可用") from exc

    def get(self, session_id: str) -> ReviewerSession | None:
        try:
            payload = self.redis.get(_session_key(self.prefix, session_id))
        except RedisError as exc:
            raise SessionStoreUnavailable("审核员 Session 服务暂时不可用") from exc
        if payload is None:
            return None
        try:
            session = ReviewerSession.from_json(payload)
            expires_at = datetime.fromisoformat(session.expires_at)
        except (TypeError, ValueError, json.JSONDecodeError):
            self.delete(session_id)
            return None
        if expires_at <= datetime.now(timezone.utc):
            self.delete(session_id)
            return None
        return session

    def delete(self, session_id: str) -> None:
        try:
            self.redis.delete(_session_key(self.prefix, session_id))
        except RedisError as exc:
            raise SessionStoreUnavailable("审核员 Session 服务暂时不可用") from exc

    def ttl(self, session_id: str) -> int:
        try:
            return int(self.redis.ttl(_session_key(self.prefix, session_id)))
        except RedisError as exc:
            raise SessionStoreUnavailable("审核员 Session 服务暂时不可用") from exc


class InMemorySessionStore:
    """Deterministic test adapter with the same absolute-expiry semantics as Redis."""

    def __init__(self):
        self._items: dict[str, tuple[ReviewerSession, datetime]] = {}
        self._lock = RLock()

    def create(self, session: ReviewerSession, ttl_seconds: int) -> None:
        with self._lock:
            self._items[session.session_id] = (
                session,
                datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
            )

    def get(self, session_id: str) -> ReviewerSession | None:
        with self._lock:
            item = self._items.get(session_id)
            if item is None:
                return None
            session, expires_at = item
            if expires_at <= datetime.now(timezone.utc):
                self._items.pop(session_id, None)
                return None
            return session

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._items.pop(session_id, None)

    def ttl(self, session_id: str) -> int:
        with self._lock:
            item = self._items.get(session_id)
            if item is None:
                return -2
            seconds = int((item[1] - datetime.now(timezone.utc)).total_seconds())
            if seconds < 0:
                self._items.pop(session_id, None)
                return -2
            return seconds

    def expire_now(self, session_id: str) -> None:
        with self._lock:
            item = self._items.get(session_id)
            if item is not None:
                self._items[session_id] = (item[0], datetime.now(timezone.utc) - timedelta(seconds=1))


class ReviewerAuthService:
    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        *,
        settings: Settings,
        session_store: SessionStore,
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.session_store = session_store
        self.password_hash = PASSWORD_HASH
        # Verification is deliberately performed even for an unknown username,
        # reducing the usefulness of response timing for account enumeration.
        self._dummy_hash = DUMMY_PASSWORD_HASH

    @staticmethod
    def normalize_username(username: str) -> str:
        return username.strip().casefold()

    def authenticate(self, username: str, password: str) -> User | None:
        normalized = self.normalize_username(username)
        with self.session_factory() as session:
            user = session.scalar(select(User).where(User.username == normalized))
            encoded = user.password_hash if user and user.password_hash else self._dummy_hash
            valid_password = self.password_hash.verify(password, encoded)
            if (
                user is None
                or not valid_password
                or not user.is_active
                or user.role != "REVIEWER"
                or not user.password_hash
            ):
                return None
            user.last_login_at = utc_now()
            session.commit()
            session.refresh(user)
            session.expunge(user)
            return user

    def issue_session(self, user: User) -> ReviewerSession:
        now = datetime.now(timezone.utc)
        session = ReviewerSession(
            session_id=secrets.token_urlsafe(32),
            reviewer_id=user.id,
            username=user.username or "",
            display_name=user.display_name,
            role=user.role,
            csrf_token=secrets.token_urlsafe(32),
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=self.settings.auth_session_ttl_seconds)).isoformat(),
        )
        self.session_store.create(session, self.settings.auth_session_ttl_seconds)
        return session

    def is_session_principal_active(self, reviewer_session: ReviewerSession) -> bool:
        with self.session_factory() as session:
            user = session.get(User, reviewer_session.reviewer_id)
            return bool(
                user
                and user.is_active
                and user.role == "REVIEWER"
                and user.username == reviewer_session.username
                and bool(user.password_hash)
            )

    @staticmethod
    def csrf_matches(session: ReviewerSession, supplied: str | None) -> bool:
        return bool(supplied) and hmac.compare_digest(session.csrf_token, supplied)


def create_session_store(settings: Settings) -> SessionStore:
    return RedisSessionStore(settings)
