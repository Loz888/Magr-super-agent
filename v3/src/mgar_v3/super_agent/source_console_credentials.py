"""Source credential encryption; file IO is server-only and outside business SQL."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet
from psycopg.pq import TransactionStatus

from mgar_v3.domain import DomainError
from mgar_v3.sources.ingress_crypto import BodyCipher

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine


class ConsoleVault:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.key_path = (
            engine.settings.artifact_root.parent / "source-console-credentials" / "key.bin"
        )

    def _outside_sql(self) -> None:
        if self.engine.connection.info.transaction_status != TransactionStatus.IDLE:
            raise DomainError(
                "CONSOLE_IO_IN_TRANSACTION", "Credential IO requires no SQL transaction", 503
            )

    @contextmanager
    def _directory(self, create: bool) -> Iterator[int]:
        self._outside_sql()
        path = self.key_path.absolute().parent
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in path.parts[1:]:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise OSError("private credential directory required")
            yield fd
        finally:
            os.close(fd)

    def _cipher(self, *, create: bool = False) -> BodyCipher:
        try:
            with self._directory(create) as directory:
                if create:
                    temporary = ".key-" + secrets.token_hex(16)
                    descriptor = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory,
                    )
                    try:
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(Fernet.generate_key())
                            stream.flush()
                            os.fsync(stream.fileno())
                        try:
                            os.link(
                                temporary,
                                self.key_path.name,
                                src_dir_fd=directory,
                                dst_dir_fd=directory,
                                follow_symlinks=False,
                            )
                        except FileExistsError:
                            pass
                    finally:
                        os.unlink(temporary, dir_fd=directory)
                    os.fsync(directory)
                descriptor = os.open(
                    self.key_path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
                )
                try:
                    info = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.geteuid()
                        or info.st_nlink != 1
                        or stat.S_IMODE(info.st_mode) != 0o600
                    ):
                        raise OSError("private regular owned key required")
                    key = os.read(descriptor, 101)
                    if len(key) > 100:
                        raise OSError("invalid key size")
                finally:
                    os.close(descriptor)
            return BodyCipher(key.strip())
        except (OSError, ValueError):
            raise DomainError(
                "CONSOLE_KEY_UNAVAILABLE", "来源凭据加密密钥不可用，请恢复本实例的安全备份。", 503
            ) from None

    def encrypt(self, values: Mapping[str, str], binding: str) -> tuple[bytes, str]:
        ciphertext = self._cipher(create=True).encrypt(
            json.dumps(dict(values), sort_keys=True, ensure_ascii=False).encode(), binding
        )
        if len(ciphertext) > 65536:
            raise DomainError("CONSOLE_CREDENTIAL_BOUND", "Credential envelope is too large", 422)
        return ciphertext, hashlib.sha256(ciphertext).hexdigest()

    def decrypt(self, ciphertext: bytes, binding: str) -> dict[str, str]:
        """Trusted server composition only; this function has no HTTP read route."""
        try:
            value = json.loads(self._cipher().decrypt(ciphertext, binding))
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ):
                raise ValueError
            return value
        except (ValueError, UnicodeError):
            raise DomainError(
                "CONSOLE_CREDENTIAL_CORRUPT", "来源凭据无法读取，请重新保存。", 503
            ) from None
