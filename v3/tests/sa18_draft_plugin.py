"""Explicit isolated-PG docs-SQL overlay for the one failed SA18 async probe.

Never loaded by the application or normal verification. The command records
PYTEST_PLUGINS explicitly; the Engine still owns its original held connection.
"""

from pathlib import Path

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    outcome = yield
    if fixturedef.argname != "engine":
        return
    engine = outcome.get_result()
    path = Path(__file__).resolve().parents[1] / (
        "docs/super-agent-v1/sa18-proposal/0051_source_review_async_intake.sql"
    )
    raw = path.read_text()
    with engine.transaction() as conn:
        name = conn.execute("SELECT current_database() AS n").fetchone()["n"]
        assert name.startswith("mgar_v3_test_")
        # Formal directory/ledger stays at 47 migrations for this draft check.
        assert conn.execute("SELECT count(*) AS n FROM app.schema_migrations").fetchone()["n"] == 47
        conn.execute(raw, prepare=False)
