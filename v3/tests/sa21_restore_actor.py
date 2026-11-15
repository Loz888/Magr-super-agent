"""Actual OS file-size failure after the original database restore has completed."""

import json
import resource
import sys
from pathlib import Path

from mgar_v3.super_agent import source_operations_backup as original

data = json.loads(Path(sys.argv[1]).read_text())
restore_pg = original.pg


def restore_then_limit(command, dsn, content=None):
    result = restore_pg(command, dsn, content)
    if command == "restore":
        # The first original hold was already persisted. The next CAS write hits
        # the kernel's real EFBIG boundary; no mocked file/PG status or exception.
        resource.setrlimit(resource.RLIMIT_FSIZE, (32, 32))
    return result


original.pg = restore_then_limit
try:
    original.restore(
        Path(data["package"]),
        Path(data["key"]),
        target_dsn=data["target_dsn"],
        target_root=Path(data["target_root"]),
        project_id=data["project_id"],
        expected_migration_sha256=data["migration_sha256"],
    )
except OSError as exc:
    # Tiny stdout pipe is unaffected by RLIMIT_FSIZE. Never print private config.
    print(json.dumps({"errno": exc.errno, "database_restored": True}), flush=True)
    raise SystemExit(23) from None
raise SystemExit("OS storage limit did not stop the original restore")
