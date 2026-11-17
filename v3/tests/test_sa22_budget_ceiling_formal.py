"""First formal0055 registration check, after named Harness ownership review."""

import hashlib
from pathlib import Path


def test_formal_forward_migration_matches_every_installed_checksum_and_preserves_0054(database):
    import psycopg

    root = Path(__file__).resolve().parents[1]
    sql = root / "migrations/0055_source_trial_budget_ceiling.sql"
    assert hashlib.sha256(sql.read_bytes()).hexdigest() == (
        "67e861128f6a8f3a01b591cfe33e1ed4e0649ae461738a7cdfb2fdf86e90a350"
    )
    assert hashlib.sha256(
        (root / "migrations/0054_source_coding_trial.sql").read_bytes()
    ).hexdigest() == ("094909bf76fe89917c4733882719f389003208ffdec4baa196461ea609ef6f48")
    expected = [
        (p.name, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted((root / "migrations").glob("*.sql"))
    ]
    with psycopg.connect(database) as conn:
        rows = conn.execute(
            "SELECT name,sha256 FROM app.schema_migrations ORDER BY name"
        ).fetchall()
        guard = conn.execute(
            "SELECT pg_get_functiondef('app.sa_coding_binding_guard()'::regprocedure)"
        ).fetchone()[0]
        check = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid="
            "'app.sa_coding_trial_authorizations'::regclass AND conname="
            "'sa_coding_trial_authorizations_maximum_project_micros_check'"
        ).fetchone()[0]
    assert rows == expected and len(rows) == 52
    assert "AND trial.maximum_project_micros IN (2000000,10000000)" in guard
    assert "2000000" in check and "10000000" in check
