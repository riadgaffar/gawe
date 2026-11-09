"""initial schema

Revision ID: 0001
Revises:
"""

from pathlib import Path

from alembic import op

from gawe.db.sqlscript import load_script, run_script

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "0001_init.sql"

# Reverse dependency order. Destructive: removes all GAWE data, including the audit log.
_TABLES = (
    "eval_metrics",
    "audit_events",
    "executions",
    "approvals",
    "policy_decisions",
    "steps",
    "plans",
    "runs",
    "artifacts",
    "tools",
    "user_roles",
    "roles",
    "users",
    "tenants",
)
_FUNCTIONS = ("audit_events_extend_chain", "audit_events_append_only")


def upgrade() -> None:
    run_script(op.get_bind(), load_script(_SQL_FILE))


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for function in _FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function}() CASCADE")
