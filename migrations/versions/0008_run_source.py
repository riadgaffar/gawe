"""A run's source record (M16c)."""

from pathlib import Path

from alembic import op

from gawe.db.sqlscript import load_script, run_script

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_script(op.get_bind(), load_script(
        Path(__file__).resolve().parents[1] / "sql/0008_run_source.sql"
    ))


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP COLUMN source_id")
