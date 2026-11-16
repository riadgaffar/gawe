"""Durable read-back lifecycle and generic maintenance jobs."""

from pathlib import Path

from alembic import op

from gawe.db.sqlscript import load_script, run_script

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_script(op.get_bind(), load_script(
        Path(__file__).resolve().parents[1] / "sql/0006_readback.sql"
    ))


def downgrade() -> None:
    # Old code cannot represent unresolved/terminal verification outcomes safely.
    raise RuntimeError("read-back lifecycle requires a reviewed data migration before downgrade")
