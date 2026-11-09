"""run state needed to resume a run after a restart

Revision ID: 0003
Revises: 0002
"""

from pathlib import Path

from alembic import op

from gawe.db.sqlscript import load_script, run_script

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "0003_run_state.sql"


def upgrade() -> None:
    run_script(op.get_bind(), load_script(_SQL_FILE))


def downgrade() -> None:
    # Destructive: drops stored findings, decisions and run errors.
    op.execute("DROP INDEX IF EXISTS runs_tenant_created_idx")
    for name in (
        "runs_no_document_when_finished",
        "runs_decisions_object",
        "runs_findings_together",
    ):
        op.execute(f"ALTER TABLE runs DROP CONSTRAINT IF EXISTS {name}")
    for column in ("error", "decisions", "findings", "findings_schema_id", "document", "roles"):
        op.execute(f"ALTER TABLE runs DROP COLUMN IF EXISTS {column}")
