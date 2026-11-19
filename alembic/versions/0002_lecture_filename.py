"""Store lecture names for display in the library."""

import sqlalchemy as sa

from alembic import op

revision = "0002_lecture_filename"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audio_files", sa.Column("filename", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("audio_files", "filename")
