"""Real kernel ENOSPC in an owned tmpfs; no simulated storage/PG result."""

import errno
import hashlib
import json
import os
import sys
from pathlib import Path

import psycopg

from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.super_agent import source_operations_backup as original
from mgar_v3.super_agent.source_operations_runtime import create_application


def main():
    data = json.loads(Path(sys.argv[1]).read_text())
    root = Path("/fault")
    assert root.is_mount() and os.statvfs(root).f_blocks * os.statvfs(root).f_frsize <= 1048576
    content = b"SA22 actual ENOSPC CAS payload\n" * 50000
    store = ArtifactStore(root / "cas")
    kept = store.put(b"previous committed CAS bytes")
    try:
        store.put(content)
    except OSError as error:
        cas_errno = error.errno
    else:
        raise AssertionError("The CAS write unexpectedly fit the dedicated tmpfs")
    assert cas_errno == errno.ENOSPC
    assert not store.path(hashlib.sha256(content).hexdigest()).exists()
    assert store.read(kept) == b"previous committed CAS bytes"
    assert not list(store.root.rglob(".pending-*"))

    restored = {}
    restore_pg = original.pg

    def observe_pg(command, dsn, incoming=None):
        result = restore_pg(command, dsn, incoming)
        if command == "restore":
            with psycopg.connect(dsn, autocommit=True) as conn:
                restored["migrations"] = conn.execute(
                    "SELECT count(*) FROM app.schema_migrations"
                ).fetchone()[0]
        return result

    original.pg = observe_pg
    target = root / "restore"
    try:
        original.restore(
            Path(data["package"]),
            Path(data["key"]),
            target_dsn=data["target_dsn"],
            target_root=target,
            project_id=data["project_id"],
            expected_migration_sha256=data["migration_sha256"],
        )
    except OSError as error:
        restore_errno = error.errno
    else:
        raise AssertionError("The original restore unexpectedly succeeded on the full tmpfs")
    assert restore_errno == errno.ENOSPC and restored["migrations"] > 0
    hold = json.loads((target / original.HOLD).read_text())
    assert hold["state"] == "INCOMPLETE_RESTORE_HOLD" and hold["activation"] is False
    assert not (target / "restore-complete.json").exists()
    os.environ.update(
        MGAR_V3_DATABASE_URL=data["target_dsn"],
        MGAR_V3_ARTIFACT_ROOT=str(target / "artifacts"),
        MGAR_V3_LOCAL_TOKEN="sa22-enospc-owned-controller-no-live-authority",
        MGAR_V3_ALLOW_PAID="0",
        MGAR_V3_ALLOW_EXTERNAL_WRITE="0",
    )
    try:
        create_application()
    except RuntimeError as error:
        assert "Restored installation is held" in str(error)
    else:
        raise AssertionError("An incomplete restore activated its API")
    print(json.dumps({
        "case_id": "F12", "cas_errno": cas_errno, "restore_errno": restore_errno,
        "database_restored": True, "migrations": restored["migrations"],
        "prior_cas_bytes_retained": True, "partial_cas_not_published": True,
        "pending_cas_removed": True, "restore_state": hold["state"],
        "activation_blocked": True, "tmpfs_max_bytes": 1048576,
    }), flush=True)


if __name__ == "__main__":
    main()
