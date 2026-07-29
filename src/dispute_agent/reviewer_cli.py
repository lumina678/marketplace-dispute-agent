from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from typing import TYPE_CHECKING, Any

from pwdlib import PasswordHash


if TYPE_CHECKING:
    from dispute_agent.models import User


USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{2,79}$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or update a reviewer workbench account")
    parser.add_argument("--username", default=os.getenv("XIANYU_REVIEWER_USERNAME"))
    parser.add_argument("--display-name", default=os.getenv("XIANYU_REVIEWER_DISPLAY_NAME", "平台审核员"))
    parser.add_argument("--user-id", default=os.getenv("XIANYU_REVIEWER_USER_ID"))
    parser.add_argument("--password-stdin", action="store_true")
    return parser


def _read_password(*, password_stdin: bool) -> str:
    configured = os.getenv("XIANYU_REVIEWER_PASSWORD")
    if configured is not None:
        return configured
    if password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Reviewer password: ")
    second = getpass.getpass("Repeat password: ")
    if first != second:
        raise ValueError("两次输入的密码不一致")
    return first


def upsert_reviewer(
    *,
    username: str,
    display_name: str,
    password: str,
    user_id: str | None = None,
    session_factory: Any = None,
) -> "User":
    # Keep CLI argument parsing (especially --help) independent from database
    # engine initialization. Runtime imports happen only when an account write
    # is actually requested.
    from sqlalchemy import select

    from dispute_agent.db import SessionLocal
    from dispute_agent.ids import new_id
    from dispute_agent.models import User

    session_factory = session_factory or SessionLocal
    normalized = username.strip().casefold()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("username 需为 3-80 位小写字母、数字、点、下划线或连字符")
    if len(password) < 12:
        raise ValueError("审核员密码至少需要 12 个字符")
    if not display_name.strip():
        raise ValueError("display_name 不能为空")

    password_hash = PasswordHash.recommended().hash(password)
    with session_factory() as session:
        user = session.scalar(select(User).where(User.username == normalized))
        if user is None and user_id:
            user = session.get(User, user_id)
        if user is not None and user.role != "REVIEWER":
            raise ValueError("目标用户已存在，但不是 REVIEWER，拒绝覆盖")
        if user is None:
            user = User(
                id=user_id or new_id("reviewer"),
                role="REVIEWER",
                display_name=display_name.strip(),
                simulated_balance_minor=0,
            )
            session.add(user)
        user.username = normalized
        user.display_name = display_name.strip()
        user.password_hash = password_hash
        user.is_active = True
        session.commit()
        session.refresh(user)
        session.expunge(user)
        return user


def main() -> None:
    args = build_parser().parse_args()
    if not args.username:
        raise SystemExit("必须通过 --username 或 XIANYU_REVIEWER_USERNAME 提供登录名")
    try:
        password = _read_password(password_stdin=args.password_stdin)
        user = upsert_reviewer(
            username=args.username,
            display_name=args.display_name,
            password=password,
            user_id=args.user_id,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"reviewer ready: id={user.id} username={user.username} display_name={user.display_name}")


if __name__ == "__main__":
    main()
