"""columns and table for the step, approval and execution stores

Revision ID: 0002
Revises: 0001
"""

from pathlib import Path

from alembic import op

from gawe.db.sqlscript import load_script, run_script

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "0002_store_columns.sql"


def upgrade() -> None:
    run_script(op.get_bind(), load_script(_SQL_FILE))


def downgrade() -> None:
    # Destructive: drops stored tool outputs and the new columns.
    op.execute("DROP TABLE IF EXISTS step_outputs CASCADE")
    op.execute("DROP FUNCTION IF EXISTS step_outputs_immutable() CASCADE")
    op.execute("ALTER TABLE executions DROP COLUMN IF EXISTS output_hash")
    op.execute("ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_approver_role_nonempty")
    op.execute("ALTER TABLE approvals DROP COLUMN IF EXISTS approver_role")
    op.execute("ALTER TABLE steps DROP COLUMN IF EXISTS error")
