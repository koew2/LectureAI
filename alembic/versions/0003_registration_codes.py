"""Store single-use registration codes and their expiry."""

import sqlalchemy as sa

from alembic import op

revision = "0003_registration_codes"
down_revision = "0002_lecture_filename"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registration_codes",
        sa.Column("email", sa.String(320), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts_left", sa.Integer(), nullable=False),
    )
    op.create_index("ix_registration_codes_expires_at", "registration_codes", ["expires_at"])


def downgrade() -> None:
    op.drop_table("registration_codes")
