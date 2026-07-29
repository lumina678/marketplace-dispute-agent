"""add reviewer authentication and trusted audit identity

Revision ID: 6f1a2b3c4d5e
Revises: e3b7a1c9d5f2
Create Date: 2026-07-29 16:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6f1a2b3c4d5e"
down_revision: Union[str, Sequence[str], None] = "e3b7a1c9d5f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("username", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("password_hash", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False))
        batch.add_column(sa.Column("last_login_at", sa.Text(), nullable=True))
        batch.create_unique_constraint("uq_users_username", ["username"])

    with op.batch_alter_table("disputes") as batch:
        batch.add_column(sa.Column("recorded_by_id", sa.String(length=80), nullable=True))
        batch.create_foreign_key(
            "fk_disputes_recorded_by_id_users",
            "users",
            ["recorded_by_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("evidence") as batch:
        batch.add_column(sa.Column("recorded_by_id", sa.String(length=80), nullable=True))
        batch.create_foreign_key(
            "fk_evidence_recorded_by_id_users",
            "users",
            ["recorded_by_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("tool_calls") as batch:
        batch.add_column(sa.Column("actor_id", sa.String(length=80), nullable=True))
        batch.create_foreign_key(
            "fk_tool_calls_actor_id_users",
            "users",
            ["actor_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("appeals") as batch:
        batch.add_column(sa.Column("recorded_by_id", sa.String(length=80), nullable=True))
        batch.create_foreign_key(
            "fk_appeals_recorded_by_id_users",
            "users",
            ["recorded_by_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            'ALTER TABLE "users" ALTER COLUMN "last_login_at" '
            'TYPE TIMESTAMP WITH TIME ZONE USING "last_login_at"::timestamptz'
        )


def downgrade() -> None:
    with op.batch_alter_table("appeals") as batch:
        batch.drop_constraint("fk_appeals_recorded_by_id_users", type_="foreignkey")
        batch.drop_column("recorded_by_id")

    with op.batch_alter_table("tool_calls") as batch:
        batch.drop_constraint("fk_tool_calls_actor_id_users", type_="foreignkey")
        batch.drop_column("actor_id")

    with op.batch_alter_table("evidence") as batch:
        batch.drop_constraint("fk_evidence_recorded_by_id_users", type_="foreignkey")
        batch.drop_column("recorded_by_id")

    with op.batch_alter_table("disputes") as batch:
        batch.drop_constraint("fk_disputes_recorded_by_id_users", type_="foreignkey")
        batch.drop_column("recorded_by_id")

    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("uq_users_username", type_="unique")
        batch.drop_column("last_login_at")
        batch.drop_column("is_active")
        batch.drop_column("password_hash")
        batch.drop_column("username")
