"""Offline, encrypted DB/CAS/key snapshots and held, isolated restoration.

No Settings/.env loading or running-Engine writes. Maintenance takes both
original locks before copying a quiesced installation. Restore never activates
listeners, old sessions, Workers, model calls or pending outgoing work.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import resource
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg.conninfo import conninfo_to_dict
from pydantic import Field, SecretStr, field_validator

from mgar_v3.domain import Model, digest
from mgar_v3.super_agent.source_operations_client import _unique, private_model
from mgar_v3.super_agent.source_operations_domain import BackupManifest, BackupPart

HEADER = b"MGAR-SA20-1\0"
MAX_BYTES = 128 * 1024 * 1024
HOLD = "restore-hold.json"


class BackupError(ValueError):
    pass


class BackupConfiguration(Model):
    database_url: SecretStr
    project_id: Annotated[str, Field(min_length=1, max_length=200)]
    artifact_root: Path
    source_key: Path
    console_key: Path
    web_key: Path
    encryption_key: Path
    offline_confirmation: str

    @field_validator("offline_confirmation")
    @classmethod
    def offline(cls, value: str) -> str:
        if value != "ALL_SERVICES_AND_JOBS_STOPPED":
            raise ValueError("An explicitly quiesced installation is required")
        return value

    @field_validator("artifact_root", "source_key", "console_key", "web_key", "encryption_key")
    @classmethod
    def absolute(cls, path: Path) -> Path:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Canonical absolute local paths required")
        return path


@contextmanager
def directory(path: Path) -> Iterator[int]:
    descriptor = os.open(path.absolute().anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.absolute().parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def read_file(path: Path, *, private: bool = False) -> bytes:
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not 0 <= info.st_size <= MAX_BYTES
            ):
                raise BackupError("Invalid backup file")
            if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise BackupError("Private key file required")
            with os.fdopen(os.dup(fd), "rb") as stream:
                value = stream.read(MAX_BYTES + 1)
            if len(value) > MAX_BYTES:
                raise BackupError("Backup capacity exceeded")
            return value
        finally:
            os.close(fd)


def key(path: Path) -> bytes:
    value = read_file(path, private=True)
    if len(value) != 32:
        raise BackupError("A separate 32-byte backup encryption key is required")
    return value


def write_new(path: Path, value: bytes) -> None:
    with directory(path.parent) as parent:
        name = "backup-new-" + uuid4().hex
        fd = os.open(
            name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(name, path.name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
        finally:
            os.unlink(name, dir_fd=parent)


def _pg_environment(dsn: str) -> dict[str, str]:
    values = conninfo_to_dict(dsn)
    # No ambient service file, options, proxy, token or platform/model credential.
    allowed = {
        "host": "PGHOST",
        "port": "PGPORT",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "dbname": "PGDATABASE",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
    }
    if set(values) - set(allowed):
        raise BackupError("Unsupported explicit database connection options")
    result = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PGCONNECT_TIMEOUT": "5"}
    result.update({allowed[k]: str(v) for k, v in values.items() if v is not None})
    return result


def pg(command: str, dsn: str, data: bytes | None = None) -> bytes:
    def capacity() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))

    env = _pg_environment(dsn)
    arguments = ["/usr/bin/pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
    if command == "restore":
        arguments = [
            "/usr/bin/pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-privileges",
            "--dbname=" + env["PGDATABASE"],
        ]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as incoming:
        if data is not None:
            incoming.write(data)
            incoming.seek(0)
        try:
            subprocess.run(
                arguments,
                env=env,
                stdin=incoming,
                stdout=output,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=120,
                preexec_fn=capacity,
            )
        except (subprocess.SubprocessError, OSError):
            raise BackupError("Database archive command failed; no activation permitted") from None
        output.seek(0)
        value = output.read(MAX_BYTES + 1)
        if len(value) > MAX_BYTES:
            raise BackupError("Backup capacity exceeded")
        return value


def migration_sha(connection: Any) -> str:
    rows = connection.execute(
        "SELECT name,sha256 FROM app.schema_migrations ORDER BY name"
    ).fetchall()
    return digest([[str(r[0]), str(r[1])] for r in rows])


def installed_sha(directory_path: Path) -> str:
    return digest(
        [
            [p.name, hashlib.sha256(p.read_bytes()).hexdigest()]
            for p in sorted(directory_path.glob("*.sql"))
        ]
    )


def project(connection: Any, expected: str) -> None:
    rows = connection.execute(
        "SELECT project_id FROM app.threads UNION SELECT project_id FROM app.mandates "
        "UNION SELECT project_id FROM app.source_instances UNION SELECT project_id "
        "FROM app.sa_operations_versions"
    ).fetchall()
    if any(r[0] != expected for r in rows):
        raise BackupError("Backup project binding does not match")


def acquire(connection: Any, lock_id: int) -> bool:
    row = connection.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,)).fetchone()
    return bool(row and row[0])


def backup(configuration: BackupConfiguration, destination: Path) -> dict[str, Any]:
    entries = {}
    kinds = {}
    encryption_key = key(configuration.encryption_key)
    dsn = configuration.database_url.get_secret_value()
    with psycopg.connect(dsn, autocommit=True) as connection:
        if not acquire(connection, 31010002):
            raise BackupError("Stop the active Engine before taking an offline backup")
        if not acquire(connection, 31010001):
            raise BackupError("A migration is in progress")
        project(connection, configuration.project_id)
        migrations = migration_sha(connection)
        entries["database.dump"] = pg("dump", dsn)
        kinds["database.dump"] = "database"
        for name, path, kind_name in (
            ("keys/source.key", configuration.source_key, "source_key"),
            ("keys/console.key", configuration.console_key, "console_key"),
            ("keys/web.key", configuration.web_key, "console_key"),
        ):
            value = read_file(path, private=True)
            Fernet(value.strip())  # Do not save an unreadable/invalid at-rest key.
            entries[name], kinds[name] = value, kind_name
        inventory = []
        with directory(configuration.artifact_root):
            for path in sorted(configuration.artifact_root.rglob("*")):
                if path.is_symlink():
                    raise BackupError("Symbolic links are not CAS artifacts")
                if path.is_dir():
                    continue
                relative = path.relative_to(configuration.artifact_root)
                if (
                    len(relative.parts) != 2
                    or relative.parts[0] != path.name[:2]
                    or (len(path.name) != 64 or any(c not in "0123456789abcdef" for c in path.name))
                ):
                    raise BackupError("CAS inventory is not quiescent or canonical")
                value = read_file(path)
                if hashlib.sha256(value).hexdigest() != path.name:
                    raise BackupError("CAS artifact hash mismatch")
                name = "cas/" + relative.as_posix()
                entries[name], kinds[name] = value, "cas"
                inventory.append(name)
                if sum(len(x) for x in entries.values()) > MAX_BYTES // 2:
                    raise BackupError("Backup capacity exceeded")
        entries["inventory/cas.json"] = json.dumps(inventory, separators=(",", ":")).encode()
        kinds["inventory/cas.json"] = "cas"
        manifest = BackupManifest(
            backup_id=uuid4(),
            project_id=configuration.project_id,
            created_at=datetime.now(UTC),
            migration_sha256=migrations,
            parts=tuple(
                BackupPart(path=n, kind=kinds[n], sha256=hashlib.sha256(v).hexdigest(), size=len(v))
                for n, v in sorted(entries.items())
            ),
        )
        entries["manifest.json"] = manifest.model_dump_json().encode()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            for name, value in sorted(entries.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(value), 0o600
                tar.addfile(info, io.BytesIO(value))
        if len(archive.getvalue()) > MAX_BYTES - 1024:
            raise BackupError("Backup capacity exceeded")
        nonce = os.urandom(12)
        encrypted = (
            HEADER + nonce + AESGCM(encryption_key).encrypt(nonce, archive.getvalue(), HEADER)
        )
        # No SQL transaction spans dump, CAS/key IO or encryption.
        assert connection.info.transaction_status.name == "IDLE"
        write_new(destination, encrypted)
    return {
        "backup_id": str(manifest.backup_id),
        "sha256": hashlib.sha256(encrypted).hexdigest(),
        "encrypted": True,
        "migration_sha256": migrations,
        "parts": len(manifest.parts),
    }


def unpack(package: Path, encryption_key: Path) -> tuple[BackupManifest, dict[str, bytes]]:
    value = read_file(package, private=True)
    if not value.startswith(HEADER):
        raise BackupError("Invalid encrypted backup header")
    nonce = value[len(HEADER) : len(HEADER) + 12]
    try:
        raw = AESGCM(key(encryption_key)).decrypt(nonce, value[len(HEADER) + 12 :], HEADER)
    except (InvalidTag, ValueError):
        raise BackupError("Backup authentication failed") from None
    entries = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            members = tar.getmembers()
            if len(members) > 100001:
                raise BackupError("Backup entry bound exceeded")
            for member in members:
                if member.name in entries or not member.isfile() or member.size > MAX_BYTES:
                    raise BackupError("Noncanonical archive entry")
                stream = tar.extractfile(member)
                if stream is None:
                    raise BackupError("Missing archive entry")
                entries[member.name] = stream.read(MAX_BYTES + 1)
        manifest = BackupManifest.model_validate(
            json.loads(entries.pop("manifest.json"), object_pairs_hook=_unique)
        )
    except (KeyError, ValueError, tarfile.TarError):
        raise BackupError("Backup manifest or archive is invalid") from None
    if set(entries) != {p.path for p in manifest.parts}:
        raise BackupError("Backup inventory differs from manifest")
    required = {
        "database.dump": "database",
        "keys/source.key": "source_key",
        "keys/console.key": "console_key",
        "keys/web.key": "console_key",
        "inventory/cas.json": "cas",
    }
    for part in manifest.parts:
        content = entries[part.path]
        if len(content) != part.size or hashlib.sha256(content).hexdigest() != part.sha256:
            raise BackupError("Backup part integrity failure")
        if part.path in required:
            if part.kind != required[part.path]:
                raise BackupError("Backup part purpose mismatch")
        elif not part.path.startswith("cas/") or part.kind != "cas":
            raise BackupError("Backup contains unsupported files")
        if part.path.startswith("cas/"):
            sections = part.path.split("/")
            if len(sections) != 3 or sections[1] != sections[2][:2] or sections[2] != part.sha256:
                raise BackupError("CAS identity mismatch")
    if not set(required) <= set(entries):
        raise BackupError("At-rest key or inventory missing")
    for name in ("keys/source.key", "keys/console.key", "keys/web.key"):
        try:
            Fernet(entries[name].strip())
        except ValueError:
            raise BackupError("Invalid restored at-rest key") from None
    if json.loads(entries["inventory/cas.json"]) != sorted(
        n for n in entries if n.startswith("cas/")
    ):
        raise BackupError("CAS inventory mismatch")
    return manifest, entries


def restore(
    package: Path,
    encryption_key: Path,
    *,
    target_dsn: str,
    target_root: Path,
    project_id: str,
    expected_migration_sha256: str,
) -> dict[str, Any]:
    manifest, entries = unpack(package, encryption_key)
    if manifest.project_id != project_id or manifest.migration_sha256 != expected_migration_sha256:
        raise BackupError("Restore project/migration binding mismatch")
    if not target_root.is_absolute() or ".." in target_root.parts:
        raise BackupError("A new absolute restore directory is required")
    # Hold marker precedes any restore IO. Errors retain this incomplete held
    # installation; never delete an operator's preexisting path or database.
    with directory(target_root.parent) as parent:
        os.mkdir(target_root.name, mode=0o700, dir_fd=parent)
    hold = {
        "state": "INCOMPLETE_RESTORE_HOLD",
        "activation": False,
        "backup_id": str(manifest.backup_id),
        "required_review": [
            "reapply_latest_revocations_and_deletions",
            "reconcile_remote_writes",
            "check_unknown_usage_jobs_leases",
            "rotate_sessions_and_service_tokens",
        ],
    }
    write_new(target_root / HOLD, json.dumps(hold).encode())
    with psycopg.connect(target_dsn, autocommit=True) as connection:
        if not acquire(connection, 31010002) or (not acquire(connection, 31010001)):
            raise BackupError("Restore target is active")
        if connection.execute(
            "SELECT 1 FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','in"
            "formation_schema') LIMIT 1"
        ).fetchone():
            raise BackupError("Restore requires a new empty database")
        pg("restore", target_dsn, entries["database.dump"])
        if migration_sha(connection) != manifest.migration_sha256:
            raise BackupError("Restored migration checksum mismatch")
        project(connection, project_id)
    for name, value in entries.items():
        if name.startswith("cas/"):
            destination = target_root / "artifacts" / name.removeprefix("cas/")
        elif name.startswith("keys/"):
            destination = target_root / name
        else:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_new(destination, value)
    # Retain the capsule's staged key inventory and install independent0600
    # files at the original at-rest consumers' canonical locations. Never use
    # hard/symbolic links for private key files.
    layout = {
        "keys/console.key": "source-console-credentials/key.bin",
        "keys/web.key": "web/credentials.key",
    }
    for source, relative in layout.items():
        destination = target_root / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_new(destination, entries[source])
    hold["state"] = "RESTORED_ON_HOLD"
    complete = target_root / "restore-complete.json"
    write_new(complete, json.dumps(hold).encode())
    with directory(target_root) as parent:
        os.rename(complete.name, HOLD, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    (target_root / "artifacts").mkdir(mode=0o700, exist_ok=True)
    return {
        "state": "RESTORED_ON_HOLD",
        "activation": False,
        "backup_id": str(manifest.backup_id),
        "migration_sha256": manifest.migration_sha256,
        "parts": len(manifest.parts),
        "latest_revocations_applied": False,
        "remote_writes_reconciled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="operation", required=True)
    save = sub.add_parser("backup")
    save.add_argument("--config", type=Path, required=True)
    save.add_argument("--output", type=Path, required=True)
    # Restore uses a separate 0600 config, never credentials in argv.
    load = sub.add_parser("restore")
    load.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "backup":
            result = backup(private_model(args.config, BackupConfiguration), args.output)
        else:
            config = private_model(args.config, RestoreConfiguration)
            result = restore(
                config.package,
                config.encryption_key,
                target_dsn=config.database_url.get_secret_value(),
                target_root=config.target_root,
                project_id=config.project_id,
                expected_migration_sha256=config.expected_migration_sha256,
            )
        print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, psycopg.Error):
        parser.exit(1, "Backup/restore failed; installation remains offline/on hold.\n")


class RestoreConfiguration(Model):
    package: Path
    encryption_key: Path
    database_url: SecretStr
    target_root: Path
    project_id: str
    expected_migration_sha256: str


if __name__ == "__main__":
    main()
