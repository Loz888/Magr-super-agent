"""Offline release inventory; optional read-only audit of this copy's local main PG."""

import argparse
import ast
import hashlib
import json
from pathlib import Path
from typing import Any

from mgar_v3.domain import Model
from mgar_v3.settings import ROOT
from mgar_v3.super_agent.source_operations_backup import BackupConfiguration, RestoreConfiguration
from mgar_v3.super_agent.source_operations_client import ServiceView, TickReceipt
from mgar_v3.super_agent.source_operations_domain import (
    BackupManifest,
    ComponentHeartbeat,
    ComponentOpen,
    ComponentTick,
    IncidentDecision,
    OperationsDefinition,
    OperationsSave,
    OperationsSnapshot,
    ServiceClient,
    ServiceSecrets,
    SignalSubmission,
)
from mgar_v3.super_agent.source_operations_gateway import GatewayConfiguration
from mgar_v3.super_agent.source_operations_relay import RelayConfiguration

SCHEMAS: dict[str, type[Model]] = {
    "definition": OperationsDefinition,
    "save": OperationsSave,
    "open": ComponentOpen,
    "heartbeat": ComponentHeartbeat,
    "tick": ComponentTick,
    "observation": SignalSubmission,
    "decision": IncidentDecision,
    "snapshot": OperationsSnapshot,
    "view": ServiceView,
    "receipt": TickReceipt,
    "client": ServiceClient,
    "secrets": ServiceSecrets,
    "backup-manifest": BackupManifest,
    "backup-config": BackupConfiguration,
    "restore-config": RestoreConfiguration,
    "gateway": GatewayConfiguration,
    "relay": RelayConfiguration,
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root: Path = ROOT) -> dict[str, Any]:
    proposal = root / "docs/super-agent-v1/sa20-proposal"
    manifest = json.loads((proposal / "proposal-manifest.json").read_text())
    ownership = json.loads((proposal / "ownership-confirmed.json").read_text())
    assert ownership["harness_confirmation"] == "1c4f494442d2d5fc8d4c1d35856cc91c922cc70e"
    for name, checksum in ownership["proposal_files_sha256"].items():
        assert sha(proposal / name) == checksum, name
    assert (root / "migrations/0053_source_operations.sql").read_bytes() == (
        proposal / "0053_source_operations.sql"
    ).read_bytes()
    style = json.loads((proposal / "shared-wiring-style-delta.json").read_text())
    assert sha(proposal / "shared-wiring-style-delta.patch") == style["delta_sha256"]
    for item in manifest["files"]:
        expected = (
            style["final_api_sha256"] if item["path"].endswith("/api.py") else item["after_sha256"]
        )
        assert sha(root.parent / item["path"]) == expected, item["path"]
    migrations = {p.name: sha(p) for p in sorted((root / "migrations").glob("*.sql"))}
    assert (
        len(migrations) == 50 and migrations["0053_source_operations.sql"] == manifest["SQL_sha256"]
    )
    catalogues = sorted((root / "config").glob("source-*-v1.json"))
    assert len(catalogues) == 19
    references = 0
    for catalogue in catalogues:
        for item in json.loads(catalogue.read_text()).get("artifacts", []):
            path = root / item["path"]
            assert path.resolve().is_relative_to(root.resolve()), item["path"]
            assert sha(path) == item["sha256"], (catalogue.name, item["path"])
            references += 1
    for name, model in SCHEMAS.items():
        path = root / "schemas/super-agent-v1" / ("source-operations-" + name + "-v1.schema.json")
        assert json.loads(path.read_text()) == model.model_json_schema(), path.name
    registry = json.loads((root / "config/verification.json").read_text())
    full = registry["SAV1-20"]["tests"]
    stages = ["CONTROL", "RECOVERY", "PROCESSES", "RUNTIME", "BOUNDARIES", "RESTORE-LAYOUT"]
    assert full == [p for stage in stages for p in registry["SAV1-20-" + stage]["tests"]]
    assert len(full) == 8 and len(set(full)) == 8
    assert all((root / path).is_file() for path in full)
    # Private role modules cannot acquire controller credentials or a second PG writer.
    for name in [
        "source_operations_client.py",
        "source_operations_gateway.py",
        "source_operations_relay.py",
    ]:
        tree = ast.parse((root / "src/mgar_v3/super_agent" / name).read_text())
        imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert not any(
            n in {"mgar_v3.settings", "mgar_v3.coordination.engine", "psycopg"} for n in imports
        )
    return {
        "card": "SAV1-20",
        "status": "PASS_OFFLINE_INVENTORY_ONLY",
        "formal_migrations": len(migrations),
        "migration_checksums": migrations,
        "source_catalogues": len(catalogues),
        "artifact_references": references,
        "operations_schemas": len(SCHEMAS),
        "formal_test_files": len(full),
        "ownership_commit": ownership["harness_confirmation"],
        "section": 46,
        "real_gates": "NOT_RUN",
        "SA21": "NOT_STARTED",
    }


def main_database() -> dict[str, Any]:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    from mgar_v3.settings import Settings

    configuration = Settings()
    dsn = configuration.database_url.get_secret_value()
    location = conninfo_to_dict(dsn)
    assert (
        location.get("host") in {"127.0.0.1", "localhost"} and str(location.get("port")) == "55435"
    )
    # Only read already installed migration and session metadata. No migrations,
    # locks, business writes, termination, probes or source/model activation.
    with psycopg.connect(
        dsn, autocommit=True, options="-c default_transaction_read_only=on"
    ) as connection:
        rows = connection.execute(
            "SELECT name,sha256 FROM app.schema_migrations ORDER BY name"
        ).fetchall()
        assert len(rows) == 44
        assert all(sha(ROOT / "migrations" / name) == checksum for name, checksum in rows)
        tables = connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema"
            "='app' AND table_name LIKE 'sa_operations_%'"
        ).fetchone()
        assert tables is not None and tables[0] == 0
    return {
        "port": 55435,
        "migrations": 44,
        "checksums_match": True,
        "operations_tables": 0,
        "read_only": True,
        "connection_closed": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--main-local", action="store_true")
    args = parser.parse_args()
    result = inventory()
    if args.main_local:
        result["main_database"] = main_database()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
