from pathlib import Path

from gawe.db.sqlscript import load_script, strip_transaction_control

MIGRATION = Path(__file__).parent.parent.parent / "migrations" / "sql" / "0001_init.sql"


def test_strips_only_bare_transaction_lines() -> None:
    sql = "BEGIN;\nCREATE TABLE t (a int);\n  COMMIT;  \nSELECT 'BEGIN;';\n"
    out = strip_transaction_control(sql)
    assert "BEGIN;\n" not in out.replace("SELECT 'BEGIN;';", "")
    assert "COMMIT" not in out
    assert "CREATE TABLE t (a int);" in out
    assert "SELECT 'BEGIN;';" in out  # a statement that merely contains the text is kept


def test_plpgsql_blocks_are_untouched() -> None:
    sql = (
        "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$\n"
        "BEGIN\n  RETURN NEW;\nEND $$;\n"
    )
    assert strip_transaction_control(sql) == sql


def test_real_migration_loses_only_its_two_transaction_lines() -> None:
    raw = MIGRATION.read_text(encoding="utf-8").splitlines()
    loaded = load_script(MIGRATION).splitlines()
    assert len(raw) - len(loaded) == 2
    assert not [line for line in loaded if line.strip() in {"BEGIN;", "COMMIT;"}]
    # the plpgsql bodies still have their BEGIN
    assert sum(1 for line in loaded if line.strip() == "BEGIN") == 2
