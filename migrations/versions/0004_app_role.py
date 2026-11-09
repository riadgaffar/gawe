"""least-privilege role for the running application

Revision ID: 0004
Revises: 0003
"""

from pathlib import Path

from alembic import op

from gawe.db.sqlscript import load_script, run_script

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "0004_app_role.sql"


def upgrade() -> None:
    run_script(op.get_bind(), load_script(_SQL_FILE))


def downgrade() -> None:
    # Removes this database's grants. The role itself is cluster-wide and may be used by other
    # databases, so it is left in place; drop it by hand if nothing else uses it.
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM gawe_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM gawe_app")
