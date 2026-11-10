"""Private, authenticated content storage and a bounded transport-only retry buffer."""

from __future__ import annotations

import base64
import fcntl
import hmac
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from mgar_v3.domain import DomainError
from mgar_v3.settings import ROOT
from mgar_v3.sources.ingress_domain import BufferPermit, DeliveryMetadata, VerifiedDelivery


class BodyCipher:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    def encrypt(self, body: bytes, binding: str) -> bytes:
        return self._fernet.encrypt(binding.encode() + b"\0" + body)

    def decrypt(self, ciphertext: bytes, binding: str) -> bytes:
        try:
            clear = self._fernet.decrypt(ciphertext)
            expected = binding.encode() + b"\0"
            if not hmac.compare_digest(clear[: len(expected)], expected):
                raise InvalidToken
            return clear[len(expected) :]
        except InvalidToken:
            raise DomainError(
                "INGRESS_CIPHERTEXT_INVALID", "Encrypted content failed integrity checks", 403
            ) from None

    @classmethod
    def from_private_file(cls, path: Path) -> BodyCipher:
        with private_directory(path.parent) as directory:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                _private_file(fd)
                key = os.read(fd, 100)
            finally:
                os.close(fd)
        try:
            return cls(key.strip())
        except ValueError:
            raise DomainError(
                "INGRESS_KEY_INVALID", "A valid private ingress key is required", 503
            ) from None


def _private_file(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise DomainError(
            "INGRESS_PRIVATE_FILE", "Ingress files must be private regular owned files", 403
        )
    return info


@contextmanager
def private_directory(path: Path) -> Iterator[int]:
    """Walk from the trusted application root using openat/NOFOLLOW; never resolve symlinks."""
    absolute = Path(os.path.abspath(path))
    try:
        relative = absolute.relative_to(ROOT)
    except ValueError:
        raise DomainError(
            "INGRESS_PRIVATE_PATH", "Ingress files must stay under this copy .local", 403
        ) from None
    if (
        not relative.parts
        or relative.parts[0] != ".local"
        or any(p in {".", ".."} for p in relative.parts)
    ):
        raise DomainError(
            "INGRESS_PRIVATE_PATH", "Ingress files must stay under this copy .local", 403
        )
    fd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(relative.parts):
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or (
                index == len(relative.parts) - 1 and stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise DomainError(
                    "INGRESS_PRIVATE_DIRECTORY", "Buffer directory must be owned and mode 0700", 403
                )
        yield fd
    finally:
        os.close(fd)


class EncryptedSpool:
    """Only an already Receive-approved BufferPermit may be stored; never ACKs a platform."""

    def __init__(
        self, path: Path, cipher: BodyCipher, *, max_records: int = 100, max_bytes: int = 4194304
    ):
        if not 1 <= max_records <= 1000 or not 1024 <= max_bytes <= 16777216:
            raise ValueError("Invalid transport buffer bound")
        self.path, self.cipher = path, cipher
        self.max_records, self.max_bytes = max_records, max_bytes

    @staticmethod
    def _name(identifier: str) -> str:
        if re.fullmatch(r"[a-f0-9]{32}", identifier) is None:
            raise DomainError("INGRESS_BUFFER_ID", "Invalid buffer identifier", 422)
        return identifier + ".bin"

    def ids(self) -> list[str]:
        with private_directory(self.path) as fd:
            return self._inventory(fd)[0]

    def _inventory(self, directory: int) -> tuple[list[str], int]:
        ids, size = [], 0
        for name in os.listdir(directory):
            if re.fullmatch(r"[a-f0-9]{32}\.bin", name) is None:
                raise DomainError("INGRESS_BUFFER_ENTRY", "Unexpected entry in private buffer", 403)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                size += _private_file(fd).st_size
            finally:
                os.close(fd)
            ids.append(name[:-4])
        return sorted(ids), size

    def put(self, permit: BufferPermit) -> str:
        identifier = uuid4().hex
        payload = {
            "generation": permit.generation,
            "identity_revisions": permit.identity_revisions,
            "expires_at": permit.expires_at.isoformat(),
            "metadata": permit.delivery.metadata.model_dump(mode="json"),
            "bodies": [
                base64.b64encode(body).decode() if body is not None else None
                for body in permit.delivery.bodies
            ],
        }
        encrypted = self.cipher.encrypt(
            json.dumps(payload, separators=(",", ":")).encode(), "spool:" + identifier
        )
        with private_directory(self.path) as directory:
            fcntl.flock(directory, fcntl.LOCK_EX)
            ids, size = self._inventory(directory)
            if len(ids) >= self.max_records or size + len(encrypted) > self.max_bytes:
                raise DomainError(
                    "INGRESS_BUFFER_FULL", "Transport buffer limit reached; no ACK", 503
                )
            fd = os.open(
                self._name(identifier),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                view = memoryview(encrypted)
                while view:
                    view = view[os.write(fd, view) :]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory)
        return identifier

    def read(self, identifier: str) -> BufferPermit:
        with private_directory(self.path) as directory:
            fd = os.open(self._name(identifier), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                info = _private_file(fd)
                if info.st_size > self.max_bytes:
                    raise DomainError(
                        "INGRESS_BUFFER_SIZE", "Buffer file exceeds its configured limit", 403
                    )
                data = bytearray()
                while chunk := os.read(fd, 65536):
                    data.extend(chunk)
                    if len(data) > self.max_bytes:
                        raise DomainError(
                            "INGRESS_BUFFER_SIZE", "Buffer file grew beyond its bound", 403
                        )
            finally:
                os.close(fd)
        try:
            payload = json.loads(self.cipher.decrypt(bytes(data), "spool:" + identifier))
            if set(payload) != {
                "generation",
                "identity_revisions",
                "expires_at",
                "metadata",
                "bodies",
            }:
                raise ValueError
            delivery = VerifiedDelivery(
                DeliveryMetadata.model_validate(payload["metadata"]),
                tuple(
                    base64.b64decode(v, validate=True) if v is not None else None
                    for v in payload["bodies"]
                ),
            )
            if type(payload["generation"]) is not int or payload["generation"] <= 0:
                raise ValueError
            expiry = datetime.fromisoformat(payload["expires_at"])
            if expiry.tzinfo is None:
                raise ValueError
            revisions = tuple((str(v[0]), int(v[1])) for v in payload["identity_revisions"])
            return BufferPermit(payload["generation"], revisions, expiry, delivery)
        except (ValueError, TypeError, KeyError, IndexError):
            raise DomainError(
                "INGRESS_BUFFER_INVALID", "Buffer record is invalid; no authority was granted", 403
            ) from None

    def remove(self, identifier: str) -> None:
        with private_directory(self.path) as directory:
            fcntl.flock(directory, fcntl.LOCK_EX)
            fd = os.open(self._name(identifier), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                _private_file(fd)
            finally:
                os.close(fd)
            os.unlink(self._name(identifier), dir_fd=directory)
            os.fsync(directory)
