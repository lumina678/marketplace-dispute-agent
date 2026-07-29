from __future__ import annotations

import argparse
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, Table, create_engine, func, inspect, select

from dispute_agent.config import get_settings
from dispute_agent.db import Base
from dispute_agent import models  # noqa: F401 - register model metadata


def _redact_url(url: str) -> str:
    if "@" not in url or "://" not in url:
        return url
    scheme, remainder = url.split("://", 1)
    return f"{scheme}://***@{remainder.split('@', 1)[1]}"


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for offset in range(0, len(rows), size):
        yield rows[offset : offset + size]


def _ordered_self_references(table: Table, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    self_foreign_keys = [
        foreign_key
        for foreign_key in table.foreign_keys
        if foreign_key.column.table.name == table.name
    ]
    if not self_foreign_keys:
        return rows
    primary_key_names = [column.name for column in table.primary_key.columns]
    if len(primary_key_names) != 1:
        raise RuntimeError(f"self-referencing table requires one primary key: {table.name}")
    primary_key = primary_key_names[0]
    pending = list(rows)
    inserted: set[Any] = set()
    ordered: list[dict[str, Any]] = []
    while pending:
        ready = [
            row
            for row in pending
            if all(
                row[foreign_key.parent.name] is None
                or row[foreign_key.parent.name] in inserted
                for foreign_key in self_foreign_keys
            )
        ]
        if not ready:
            raise RuntimeError(f"cyclic self-reference detected in table: {table.name}")
        ordered.extend(ready)
        inserted.update(row[primary_key] for row in ready)
        ready_ids = {id(row) for row in ready}
        pending = [row for row in pending if id(row) not in ready_ids]
    return ordered


def _assert_schema(engine: Engine, *, label: str) -> None:
    tables = set(inspect(engine).get_table_names())
    missing = set(Base.metadata.tables) - tables
    if missing:
        raise RuntimeError(f"{label} schema is missing tables: {', '.join(sorted(missing))}")


def _target_has_data(engine: Engine) -> bool:
    with engine.connect() as connection:
        return any(connection.scalar(select(func.count()).select_from(table)) for table in Base.metadata.sorted_tables)


def migrate(source_url: str, target_url: str, *, batch_size: int, dry_run: bool) -> dict[str, int]:
    if not source_url.startswith("sqlite:///"):
        raise ValueError("source must be a sqlite:/// URL")
    if not target_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("target must be a PostgreSQL URL")
    source = create_engine(source_url, future=True)
    target = create_engine(target_url, future=True, pool_pre_ping=True)
    try:
        _assert_schema(source, label="source")
        _assert_schema(target, label="target")
        if _target_has_data(target):
            raise RuntimeError("target database is not empty; refusing to duplicate or overwrite data")
        counts: dict[str, int] = {}
        with source.connect() as source_connection:
            for table in Base.metadata.sorted_tables:
                counts[table.name] = int(source_connection.scalar(select(func.count()).select_from(table)) or 0)
        if dry_run:
            return counts
        with source.connect() as source_connection, target.begin() as target_connection:
            for table in Base.metadata.sorted_tables:
                rows = [dict(row) for row in source_connection.execute(select(table)).mappings()]
                rows = _ordered_self_references(table, rows)
                for chunk in _chunks(rows, batch_size):
                    target_connection.execute(table.insert(), chunk)
        with target.connect() as target_connection:
            copied = {
                table.name: int(target_connection.scalar(select(func.count()).select_from(table)) or 0)
                for table in Base.metadata.sorted_tables
            }
        mismatches = {
            table: (counts[table], copied[table])
            for table in counts
            if counts[table] != copied[table]
        }
        if mismatches:
            raise RuntimeError(f"row count verification failed: {mismatches}")
        return copied
    finally:
        source.dispose()
        target.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy a migrated Xianyu SQLite database into empty PostgreSQL")
    parser.add_argument("--source", required=True, help="source sqlite:/// URL")
    parser.add_argument("--target", default=None, help="target PostgreSQL URL; defaults to XIANYU_DATABASE_URL")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    target_url = args.target or get_settings().database_url
    source_path = Path(args.source.removeprefix("sqlite:///")).expanduser()
    if not source_path.exists():
        parser.error(f"SQLite source does not exist: {source_path}")
    print(f"source={source_path.resolve()}")
    print(f"target={_redact_url(target_url)}")
    counts = migrate(args.source, target_url, batch_size=args.batch_size, dry_run=args.dry_run)
    action = "validated" if args.dry_run else "copied"
    print(f"{action}_tables={len(counts)} {action}_rows={sum(counts.values())}")
    for table_name, count in counts.items():
        print(f"{table_name}: {count}")


if __name__ == "__main__":
    main()
