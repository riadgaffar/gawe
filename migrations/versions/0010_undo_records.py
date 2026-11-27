"""Undo records written before each undoable write (M26)."""

from pathlib import Path

from alembic import op

from gawe.db.sqlscript import load_script, run_script

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_script(op.get_bind(), load_script(
        Path(__file__).resolve().parents[1] / "sql/0010_undo_records.sql"
    ))


def downgrade() -> None:
    op.execute("DROP TABLE undo_records")
