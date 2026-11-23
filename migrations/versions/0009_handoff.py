"""Hand-offs for prepare-only actions (M20)."""

from pathlib import Path

from alembic import op

from gawe.db.sqlscript import load_script, run_script

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_script(op.get_bind(), load_script(
        Path(__file__).resolve().parents[1] / "sql/0009_handoff.sql"
    ))


def downgrade() -> None:
    # Only a database with no hand-offs can go back: the old states can't represent them.
    rows = op.get_bind().exec_driver_sql(
        "SELECT count(*) FROM steps WHERE handoff IS NOT NULL "
        "OR status IN ('prepared', 'done_by_person', 'not_done_by_person')"
    ).scalar()
    runs = op.get_bind().exec_driver_sql(
        "SELECT count(*) FROM runs WHERE status = 'waiting_for_person'"
    ).scalar()
    if rows or runs:
        raise RuntimeError("hand-offs exist; downgrading would lose them")
    op.execute("ALTER TABLE steps DROP CONSTRAINT steps_handoff_state_check")
    op.execute("ALTER TABLE steps DROP COLUMN handoff")
    op.execute("ALTER TABLE steps DROP CONSTRAINT steps_status_check")
    op.execute(
        "ALTER TABLE steps ADD CONSTRAINT steps_status_check CHECK (status IN ("
        "'pending', 'ready', 'policy_checked', 'awaiting_approval', 'approved', "
        "'rejected', 'denied', 'executing', 'verifying', 'awaiting_readback', "
        "'verification_blocked', 'verification_unknown', 'succeeded', 'failed'))"
    )
    op.execute("ALTER TABLE runs DROP CONSTRAINT runs_status_check")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT runs_status_check CHECK (status IN ("
        "'created', 'planning', 'running', 'paused_for_approval', 'waiting_verification', "
        "'completed', 'failed', 'cancelled', 'denied', 'unknown'))"
    )
