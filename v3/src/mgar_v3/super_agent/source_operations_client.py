"""Limited local service process; imports neither Settings nor Engine/PG clients."""

import argparse
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import Field

from mgar_v3.domain import Model
from mgar_v3.super_agent.source_operations_domain import (
    ComponentHeartbeat,
    ComponentOpen,
    ComponentTick,
    Counter,
    OperationsDefinition,
    OperationsSnapshot,
    ServiceClient,
    Sha256,
    SignalSubmission,
    signals,
)

MAX_BYTES = 65536


class PrivateConfigurationError(ValueError):
    pass


class ServiceUnavailable(Exception):
    pass


class SubmissionUnconfirmed(ServiceUnavailable):
    pass


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def private_model[M: Model](path: Path, model: type[M]) -> M:
    """Open every path component without symlink following and bound credential IO."""
    directory = None
    fd = None
    try:
        absolute = path.absolute()
        directory = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in absolute.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(absolute.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 0 < info.st_size <= MAX_BYTES
        ):
            raise ValueError("private bounded file required")
        value = bytearray()
        while len(value) <= MAX_BYTES:
            chunk = os.read(fd, min(8192, MAX_BYTES + 1 - len(value)))
            if not chunk:
                break
            value.extend(chunk)
        if len(value) > MAX_BYTES:
            raise ValueError("configuration bound")
        return model.model_validate(json.loads(value, object_pairs_hook=_unique))
    except (OSError, ValueError):
        raise PrivateConfigurationError("Invalid private service configuration") from None
    finally:
        if fd is not None:
            os.close(fd)
        if directory is not None:
            os.close(directory)


class TickReceipt(Model):
    state: Literal["idle", "reserved", "known", "unknown"] = "idle"
    sequence: Counter = 0
    request_id: UUID | None = None
    processed: Counter = 0


class ServiceView(Model):
    definition: OperationsDefinition | None
    snapshot: OperationsSnapshot
    snapshot_sha256: Sha256
    tick: TickReceipt = Field(default_factory=TickReceipt)
    driver_available: bool = Field(default=False, strict=True)


class LocalOperationsClient:
    def __init__(self, config: ServiceClient, *, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.http = httpx.Client(
            base_url=config.api_url,
            timeout=5,
            follow_redirects=False,
            trust_env=False,
            headers={"X-MGAR-Operations-Token": config.token.get_secret_value()},
            transport=transport,
        )
        self.boot_id = uuid4()
        self.open_id = uuid4()
        self.unconfirmed_tick: UUID | None = None

    def close(self) -> None:
        self.http.close()

    def request(self, operation: str, body: Model | None = None) -> dict[str, Any]:
        methods = {
            "snapshot": "GET",
            "open": "POST",
            "heartbeat": "POST",
            "tick": "POST",
            "observations": "POST",
        }
        if operation not in methods:
            raise ValueError("Operation is outside this limited client")
        method = methods[operation]
        path = f"/api/v3/source-operations/components/{self.config.component_id}/{operation}"
        try:
            payload = body.model_dump(mode="json") if body is not None else None
            with self.http.stream(method, path, json=payload) as response:
                if response.status_code != 200:
                    if method == "POST" and response.status_code >= 500:
                        raise SubmissionUnconfirmed
                    raise ServiceUnavailable
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > MAX_BYTES:
                        raise ValueError("response bound")
                    raw.extend(chunk)
                value = json.loads(raw, object_pairs_hook=_unique)
                if not isinstance(value, dict):
                    raise ValueError("object response required")
                return value
        except (httpx.HTTPError, ValueError):
            if method == "POST":
                raise SubmissionUnconfirmed from None
            raise ServiceUnavailable from None

    def poll(self) -> dict[str, str | int | bool | None]:
        view = ServiceView.model_validate(self.request("snapshot"))
        if self.unconfirmed_tick is not None:
            if view.tick.request_id == self.unconfirmed_tick and view.tick.state == "known":
                self.unconfirmed_tick = None
                return {"status": "reconciled", "resubmitted": False}
            return {"status": "awaiting_reconciliation", "saved": None, "resubmitted": False}
        definition = view.definition
        grant = (
            next(
                (g for g in definition.components if g.component_id == self.config.component_id),
                None,
            )
            if definition
            else None
        )
        if (
            definition is None
            or grant is None
            or not definition.enabled
            or not grant.enabled
            or definition.valid_until <= view.snapshot.observed_at
        ):
            return {"status": "inactive", "model_calls": 0}
        if grant.role != self.config.role:
            raise ServiceUnavailable
        component = next(
            (c for c in view.snapshot.components if c.component_id == grant.component_id), None
        )
        if component is None or component.boot_id != self.boot_id:
            if view.tick.state in {"reserved", "unknown"}:
                return {"status": "awaiting_reconciliation", "saved": None, "resubmitted": False}
            self.request(
                "open",
                ComponentOpen(
                    config_revision=view.snapshot.config_revision,
                    expected_generation=component.generation if component else 0,
                    boot_id=self.boot_id,
                    request_id=self.open_id,
                ),
            )
            view = ServiceView.model_validate(self.request("snapshot"))
            component = next(
                (c for c in view.snapshot.components if c.component_id == grant.component_id), None
            )
        if component is None or component.boot_id != self.boot_id:
            raise ServiceUnavailable
        state: Literal["ready", "waiting_runner"] = (
            "waiting_runner" if grant.role == "worker" and not view.driver_available else "ready"
        )
        self.request(
            "heartbeat",
            ComponentHeartbeat(
                generation=component.generation,
                boot_id=self.boot_id,
                sequence=component.sequence + 1,
                state=state,
            ),
        )
        if view.tick.state in {"reserved", "unknown"}:
            return {"status": "awaiting_reconciliation", "saved": None, "resubmitted": False}
        if grant.role == "source":
            return {"status": "observed", "platform_connection_confirmed": False}
        if grant.role == "watchdog":
            # Re-read after the heartbeat: it is a liveness fact, not progress.
            view = ServiceView.model_validate(self.request("snapshot"))
            assert view.definition is not None
            candidates = signals(view.snapshot, view.definition)[:32]
            for fact in candidates:
                self.request(
                    "observations",
                    SignalSubmission(
                        kind=fact.kind,
                        subject_id=fact.subject_id,
                        observation_key=fact.key(view.snapshot.project_id),
                        snapshot_sha256=view.snapshot_sha256,
                    ),
                )
            return {
                "status": "observed",
                "signals": len(candidates),
                "partial": view.snapshot.partial,
            }
        identifier = uuid4()
        try:
            result = self.request(
                "tick",
                ComponentTick(
                    sequence=view.tick.sequence + 1,
                    generation=component.generation,
                    boot_id=self.boot_id,
                    request_id=identifier,
                ),
            )
            receipt = TickReceipt.model_validate(result)
            if receipt.request_id != identifier:
                raise SubmissionUnconfirmed
        except (SubmissionUnconfirmed, ValueError):
            self.unconfirmed_tick = identifier
            return {"status": "awaiting_reconciliation", "saved": None, "resubmitted": False}
        if receipt.state in {"reserved", "unknown"}:
            self.unconfirmed_tick = identifier
            return {"status": "awaiting_reconciliation", "saved": None, "resubmitted": False}
        if receipt.state != "known":
            raise ServiceUnavailable
        return {"status": "observed", "processed": receipt.processed, "tick_state": receipt.state}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        config = private_model(args.config, ServiceClient)
    except PrivateConfigurationError:
        print(json.dumps({"status": "invalid_private_configuration"}))
        return 2
    client = LocalOperationsClient(config)
    try:
        while True:
            try:
                result = client.poll()
                code = 0 if result["status"] in {"observed", "inactive", "reconciled"} else 1
            except (ServiceUnavailable, ValueError):
                result, code = {"status": "unavailable"}, 1
            print(json.dumps(result), flush=True)
            if args.once:
                return code
            time.sleep(config.interval_seconds)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
