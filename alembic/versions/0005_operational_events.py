"""Store structured events for the administrator's logs."""

import sqlalchemy as sa

from alembic import op

revision = "0005_operational_events"
down_revision = "0004_admin_processing_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operational_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("level", sa.String(5), nullable=False),
        sa.Column("event", sa.String(40), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("lecture_id", sa.Uuid(), sa.ForeignKey("audio_files.id", ondelete="SET NULL")),
        sa.Column("error_type", sa.String(80)),
        sa.Column("status_code", sa.Integer()),
    )
    op.create_index("ix_operational_events_level", "operational_events", ["level"])


def downgrade() -> None:
    op.drop_table("operational_events")
