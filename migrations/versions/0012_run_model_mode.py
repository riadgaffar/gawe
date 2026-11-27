"""A run's scripted demo model mode (M29)."""

from pathlib import Path

from alembic import op

from gawe.db.sqlscript import load_script, run_script

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_script(
        op.get_bind(),
        load_script(Path(__file__).resolve().parents[1] / "sql/0012_run_model_mode.sql"),
    )


def downgrade() -> None:
    # Drops only the demo runs' modes; nothing else reads the column.
    op.execute("ALTER TABLE runs DROP COLUMN model_mode")
