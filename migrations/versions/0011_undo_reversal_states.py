"""Allow reversal lifecycle states on existing undo-record databases."""

from pathlib import Path
from alembic import op
from gawe.db.sqlscript import load_script, run_script

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_script(
        op.get_bind(),
        load_script(Path(__file__).resolve().parents[1] / "sql/0011_undo_reversal_states.sql"),
    )


def downgrade() -> None:
    # Current 0010 already accepts these states; retain its identical constraints.
    pass
