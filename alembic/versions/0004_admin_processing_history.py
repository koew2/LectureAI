"""Add registration names and retain lecture processing outcomes."""

import sqlalchemy as sa

from alembic import op

revision = "0004_admin_processing_history"
down_revision = "0003_registration_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(80), nullable=True))
    op.create_table(
        "lecture_processing_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lecture_id", sa.Uuid(), nullable=False),
        sa.Column("successful", sa.Boolean(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["lecture_id"], ["audio_files.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_lecture_processing_results_lecture_id", "lecture_processing_results", ["lecture_id"]
    )
    op.execute(
        "INSERT INTO lecture_processing_results (lecture_id, successful, error_message) "
        "SELECT id, status = 'completed', error_message FROM audio_files "
        "WHERE status IN ('completed', 'failed') ORDER BY created_at, id"
    )


def downgrade() -> None:
    op.drop_table("lecture_processing_results")
    op.drop_column("users", "username")
