"""Next-day review: tenant-scoped jobs, review items and an audit index."""

from pathlib import Path

from alembic import op

from gawe.db.sqlscript import load_script, run_script

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_script(op.get_bind(), load_script(
        Path(__file__).resolve().parents[1] / "sql/0007_review.sql"
    ))


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS audit_events_type_ts_idx")
    op.execute("DROP TABLE IF EXISTS review_items")
    op.execute("DELETE FROM maintenance_jobs WHERE run_id IS NULL")
    op.execute("ALTER TABLE maintenance_jobs DROP CONSTRAINT maintenance_jobs_tenant_scope")
    op.execute("ALTER TABLE maintenance_jobs DROP CONSTRAINT maintenance_jobs_run_record")
    op.execute(
        "ALTER TABLE maintenance_jobs ADD CHECK (record->>'run_id' = run_id::text)"
    )
    op.execute("ALTER TABLE maintenance_jobs ALTER COLUMN run_id SET NOT NULL")
