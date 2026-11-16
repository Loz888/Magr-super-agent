"""Short-lived reply keys; original usage/Action records contain ciphertext and hashes."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from cryptography.fernet import Fernet

from mgar_v3.domain import DomainError
from mgar_v3.sources.ingress_crypto import BodyCipher, _private_file, private_directory


class ReplyKeys:
    def __init__(self, root: Path):
        self.root = root

    def seal(self, call_id: UUID, text: str, expires: datetime) -> str:
        if expires <= datetime.now(UTC):
            raise DomainError("SOURCE_CHAT_EXPIRED", "Reply retention has expired", 403)
        key = Fernet.generate_key()
        value = json.dumps({"key": key.decode(), "expires_at": expires.isoformat()}).encode()
        with private_directory(self.root) as directory:
            fd = os.open(
                call_id.hex + ".json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                os.write(fd, value)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(directory)
        return BodyCipher(key).encrypt(text.encode(), str(call_id)).decode()

    @staticmethod
    def _read(directory: int, name: str) -> dict[str, str]:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        try:
            if _private_file(fd).st_size > 512:
                raise ValueError
            value = json.loads(os.read(fd, 513))
        finally:
            os.close(fd)
        if not isinstance(value, dict) or set(value) != {"key", "expires_at"}:
            raise ValueError
        return value

    def open(self, call_id: UUID, ciphertext: str) -> str:
        try:
            with private_directory(self.root) as directory:
                value = self._read(directory, call_id.hex + ".json")
            if datetime.fromisoformat(value["expires_at"]) <= datetime.now(UTC):
                raise ValueError
            return (
                BodyCipher(value["key"].encode())
                .decrypt(ciphertext.encode(), str(call_id))
                .decode()
            )
        except (OSError, ValueError, UnicodeError, TypeError, KeyError):
            raise DomainError("SOURCE_CHAT_KEY_UNAVAILABLE", "Reply key has expired", 403) from None

    def purge(self) -> int:
        count = 0
        with private_directory(self.root) as directory:
            for name in os.listdir(directory):
                if len(name) != 37 or not name.endswith(".json"):
                    raise DomainError("SOURCE_CHAT_KEY_DIRECTORY", "Unexpected key entry", 403)
                try:
                    UUID(hex=name[:-5])
                    value = self._read(directory, name)
                    expired = datetime.fromisoformat(value["expires_at"]) <= datetime.now(UTC)
                except (ValueError, KeyError, TypeError):
                    raise DomainError("SOURCE_CHAT_KEY_DIRECTORY", "Invalid private key entry", 403)
                if expired:
                    os.unlink(name, dir_fd=directory)
                    count += 1
            os.fsync(directory)
        return count
