"""Trusted preparation and immutable approval snapshots."""

from pathlib import Path
from alembic import op
from gawe.db.sqlscript import load_script, run_script

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_script(
        op.get_bind(),
        load_script(Path(__file__).resolve().parents[1] / "sql/0005_prepared_actions.sql"),
    )


def downgrade() -> None:
    op.execute("ALTER TABLE approvals DROP COLUMN resolved_args, DROP COLUMN preparation")
    op.execute("ALTER TABLE steps DROP COLUMN policy_decision, DROP COLUMN preparation")
