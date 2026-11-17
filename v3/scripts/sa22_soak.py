"""Zero-paid SA22 controlled soak, using original services and an isolated PG.

The config is a 0600 local file created for this run. No .env, live platform or
supplier credential is loaded. Tests/ supplies only the declared synthetic
installation seeding, never a mocked Engine, SQL store, CAS or execution broker.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import signal
import stat
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr
from sa22_acceptance import inspect_journal, seal_record
from verify import source_fingerprint

from mgar_v3.coordination.engine import Engine
from mgar_v3.effects.dispatcher import Dispatcher
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.harness.broker import IsolatedExecutionBroker, issue_lease
from mgar_v3.harness.contracts import CancellationToken, CapabilityGrant, ExecutionRequest
from mgar_v3.settings import Settings
from mgar_v3.sources.domain import ActorRef
from mgar_v3.sources.feishu_domain import FeishuActivation, FeishuSecrets
from mgar_v3.sources.feishu_runtime import FeishuRuntime
from mgar_v3.sources.feishu_store import FeishuSources
from mgar_v3.sources.ingress_crypto import BodyCipher
from mgar_v3.sources.ingress_domain import IngressConfigure, IngressSettings
from mgar_v3.sources.ports import TransportEnvelope
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_feishu_channel import FeishuNotificationChannel
from mgar_v3.super_agent.source_notification_domain import NotificationConfigure
from mgar_v3.super_agent.source_notification_ports import NotificationProfile
from mgar_v3.super_agent.source_notification_transport import NotificationTransport
from mgar_v3.super_agent.source_replies import queue_reply

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from sa06_support import BOT_ID, CHAT_ID, payload, signed  # noqa: E402
from sa22_reply_support import compose  # noqa: E402


def save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".new")
    with temporary.open("w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def configuration(path: Path) -> dict:
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("A private regular config is required")
    value = json.loads(path.read_text())
    dsn = conninfo_to_dict(value["database_url"])
    if (
        value.get("purpose") != "SA22_CONTROLLED_SOAK"
        or dsn.get("host") != "127.0.0.1"
        or not dsn.get("dbname", "").startswith("sa22_soak_")
        or not value["image_id"].startswith("sha256:")
    ):
        raise ValueError("Dedicated controlled resources required")
    return value


class Workload:
    def __init__(self, config: dict, root: Path):
        self.config, self.root = config, root
        self.platform_requests = []
        self.lose_reply = False
        self.crash_after_ingress = False
        self.server = None
        self.engine = Engine(
            Settings(
                _env_file=None,
                database_url=config["database_url"],
                local_token=SecretStr("sa22-controlled-soak-not-a-live-token"),
                artifact_root=root / "cas",
                project_id=config["project_id"],
                repository="example/mgar-test",
                local_actor="owner",
                allowed_actors=["owner"],
                graph_version="r1-quotes-v1",
                allow_paid=False,
                allow_external_write=False,
            ),
            ArtifactStore(root / "cas"),
        )
        try:
            self._start_http()
            self._restore_or_seed()
        except BaseException:
            self.close()
            raise

    def _start_http(self):
        workload = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    self.send_error(413)
                    return
                raw = self.rfile.read(size)
                if self.path == "/ingress":
                    receipt = workload.runtime.receive(TransportEnvelope(dict(self.headers), raw))
                    result = receipt.model_dump(mode="json")
                elif self.path.endswith("/tenant_access_token/internal"):
                    result = {"code": 0, "tenant_access_token": "synthetic-only"}
                elif self.path.endswith("/reply"):
                    body = json.loads(raw)
                    workload.platform_requests.append(body["uuid"])
                    remote = "om_soak_" + body["uuid"]
                    # Real HTTP closes after acceptance, before response, for the fault case.
                    if workload.lose_reply:
                        self.close_connection = True
                        return
                    result = {"code": 0, "data": {"message_id": remote, "chat_id": CHAT_ID}}
                else:
                    self.send_error(404)
                    return
                data = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def _restore_or_seed(self):
        seed_path = self.root / "seed-private.json"
        expires = datetime.fromisoformat(self.config["expires_at"])
        seeded_profile = None
        if not seed_path.exists():
            env = compose(self.engine)
            seeded_profile = env.profile
            current = self.engine.source_policies.read(env.source.cookie, env.source.policy_id)
            raw = current["definition"]
            raw["valid_until"] = expires.isoformat()
            policy = self.engine.source_policies.save(
                env.source.cookie,
                env.source.policy_id,
                PolicySave(
                    expected_revision=2,
                    definition=PolicyDefinition.model_validate(raw),
                    review_ref="SA22-controlled-72h-policy-no-model-no-code",
                ),
            )
            self.engine.source_ingress.configure(
                env.source.cookie,
                env.source.source_id,
                IngressConfigure(
                    expected_generation=2,
                    settings=IngressSettings(
                        owner="gateway", policy_id=env.source.policy_id, policy_revision=3
                    ),
                    review_ref="SA22-controlled-72h-ingress",
                ),
            )
            settings = env.config.model_copy(
                update={
                    "policy_revision": 3,
                    "policy_sha256": policy["definition_sha256"],
                    "valid_until": expires,
                }
            )
            self.engine.source_notifications.configure(
                env.source.cookie,
                env.source.source_id,
                NotificationConfigure(
                    expected_revision=1,
                    settings=settings,
                    review_ref="SA22-controlled-local-HTTP-only",
                ),
            )
            cipher = self.engine.source_ingress.cipher._fernet
            # Export only this freshly generated synthetic fixture key for process recovery.
            # No application credential, production key or real message is involved.
            seed = {
                "source_id": str(env.source.source_id),
                "cookie": env.source.cookie,
                "body_key": base64.urlsafe_b64encode(
                    cipher._signing_key + cipher._encryption_key
                ).decode(),
                "secrets": {
                    key: getattr(env.source.secrets, key)
                    for key in ("app_secret", "verification_token", "encrypt_key")
                },
                "seed_input_excluded": str(env.input_id),
            }
            save(seed_path, seed)
        seed = json.loads(seed_path.read_text())
        self.cookie, self.source_id = seed["cookie"], UUID(seed["source_id"])
        self.secrets = FeishuSecrets(**seed["secrets"])
        self.engine.source_ingress.cipher = BodyCipher(seed["body_key"].encode())
        self.runtime = FeishuRuntime(
            FeishuSources(self.engine),
            self.source_id,
            2,
            3,
            self.secrets,
            FeishuActivation(
                self.source_id,
                2,
                3,
                expires,
                frozenset({"webhook", "socket", "health"}),
                "SA22-controlled-72h",
            ),
            transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        )
        instance = SourceRegistry(self.engine).get_instance(self.source_id)

        def relay(request):
            with httpx.Client(trust_env=False, timeout=5) as client:
                response = client.post(
                    self.url + request.url.path,
                    content=request.content,
                    headers={"Content-Type": "application/json"},
                )
            return httpx.Response(response.status_code, content=response.content)

        if seeded_profile is not None:
            # Keep the exact trusted channel already registered by the seeder.
            # Only its controlled physical transport is composed with local HTTP.
            profile = seeded_profile
            profile.channel.transport = httpx.MockTransport(relay)
        else:
            channel = FeishuNotificationChannel(
                self.engine,
                instance.scope,
                lambda: self.secrets,
                mode="fixture",
                transport=httpx.MockTransport(relay),
            )
            profile = NotificationProfile(
                instance.scope,
                instance.revision,
                ActorRef(
                    scope=instance.scope, id_type="open_id", actor_id=BOT_ID, actor_kind="bot"
                ),
                instance.channel_capabilities,
                channel,
            )
        self.engine.source_notifications.register_profile(profile)
        self.dispatcher = Dispatcher(
            self.engine, NotificationTransport(self.engine.source_notifications)
        )

    def cycle(self) -> dict:
        pending_path = self.root / "pending-cycle.json"
        if pending_path.exists():
            pending = json.loads(pending_path.read_text())
        else:
            pending = {
                "event": payload(
                    message_id="om_soak_" + uuid4().hex, body="SA22 合成接收与状态负载 @_user_1"
                ),
                "phase": "planned",
            }
            save(pending_path, pending)
        if pending["phase"] not in {"planned", "committed"}:
            raise RuntimeError("UNRESOLVED_CYCLE_NO_RESEND")
        envelope = signed(SimpleNamespace(secrets=self.secrets), pending["event"])
        started = time.monotonic()
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.post(
                self.url + "/ingress", content=envelope.payload, headers=envelope.headers
            )
        response.raise_for_status()
        receipt = response.json()
        if receipt["disposition"] not in {"accepted", "duplicate"} or not receipt["input_ids"]:
            raise RuntimeError("CONTROLLED_INPUT_NOT_COMMITTED")
        input_id = UUID(receipt["input_ids"][0])
        pending.update(phase="committed", input_id=str(input_id))
        save(pending_path, pending)
        if self.crash_after_ingress:
            os.kill(os.getpid(), signal.SIGKILL)
        plan = queue_reply(self.engine, self.cookie, input_id)
        self.dispatcher.tick()
        with self.engine.transaction() as conn:
            action = conn.execute(
                "SELECT a.status,o.attempts FROM app.actions a JOIN app.outbox o "
                "USING(action_id) WHERE a.action_id=%s",
                (plan["action_id"],),
            ).fetchone()
        if action["status"] != "succeeded":
            pending.update(phase="unknown" if action["status"] == "unknown" else "failed")
            save(pending_path, pending)
            raise RuntimeError("CONTROLLED_DELIVERY_" + action["status"].upper())
        if action["attempts"] != 1:
            raise RuntimeError("DUPLICATE_CONTROLLED_DISPATCH")
        result = {
            "input_id": str(input_id),
            "run_id": str(plan["run_id"]),
            "action_id": str(plan["action_id"]),
            "attempts": action["attempts"],
            "ingress_and_reply_seconds": time.monotonic() - started,
            "platform": "controlled_local_HTTP",
            "model_calls": 0,
        }
        content = json.dumps(result, sort_keys=True).encode()
        result["artifact_sha256"] = self.engine.artifacts.put(content)
        if self.engine.artifacts.read(result["artifact_sha256"]) != content:
            raise RuntimeError("CAS_MISMATCH")
        with (self.root / "cycles.jsonl").open("a") as stream:
            stream.write(json.dumps(result) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        pending_path.unlink()
        self.engine.source_ingress.purge(self.cookie, self.source_id)
        return result

    def sandbox(self, number: int) -> dict:
        workspace = self.root / "sandbox"
        code = workspace / "src"
        code.mkdir(parents=True, exist_ok=True)
        code.chmod(0o755)
        (code / "probe.py").write_text(
            "import os\nassert os.geteuid() != 0\n"
            f"assert sum(range({number + 4})) == {(number + 3) * (number + 4) // 2}\n"
            "print('SA22_CONTROLLED_SANDBOX_OK')\n"
        )
        grant = CapabilityGrant(
            actor="owner",
            workspace_id="sa22-controlled-soak",
            repository="example/mgar-test",
            read_paths=("src",),
            write_paths=(),
            execute=True,
            allowed_commands=("python3",),
            max_seconds=20,
        )
        lease = issue_lease(
            mode="investigate",
            actor=grant.actor,
            workspace_id=grant.workspace_id,
            repository=grant.repository,
            execution_epoch=number + 1,
            read_paths=("src",),
            allowed_commands=("python3",),
            timeout_seconds=20,
            max_calls=1,
            ttl_seconds=30,
        )
        broker = IsolatedExecutionBroker(workspace, lease, image_id=self.config["image_id"])
        receipt = broker.execute(
            ExecutionRequest(
                operation_id="sa22-soak-" + str(number),
                argv=("python3", "probe.py"),
                cwd="src",
                timeout_seconds=20,
                execution_epoch=lease.execution_epoch,
            ),
            grant,
            CancellationToken(),
        )
        if receipt.status != "passed" or receipt.exit_code != 0:
            raise RuntimeError("CONTROLLED_SANDBOX_FAILED")
        artifact = self.engine.artifacts.put(receipt.model_dump_json().encode())
        self.engine.artifacts.read(artifact)
        return {
            "artifact_sha256": artifact,
            "tree_hash": broker.workspace_tree_hash(),
            "image_id": self.config["image_id"],
            "scope": "component_health_not_task_repair",
        }

    def close(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(5)
        self.engine.close()


def run(path: Path, *, once=False, crash=False):
    config = configuration(path)
    root = path.parent
    with (root / "runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        segment = root / (
            "segment-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:6]
        )
        segment.mkdir(mode=0o700)
        records, cycles, probes = [], 0, 0
        fingerprint = source_fingerprint(ROOT)
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        save(
            root / "launch.json",
            {"pid": os.getpid(), "segment": str(segment), "UTC": datetime.now(UTC).isoformat()},
        )
        workload = Workload(config, root)
        workload.crash_after_ingress = crash
        next_event = next_sandbox = time.monotonic()
        try:
            while not stop.is_set():
                now = time.monotonic()
                facts = {}
                status = "healthy"
                try:
                    if now >= next_event:
                        facts["cycle"] = workload.cycle()
                        cycles += 1
                        next_event = time.monotonic() + 300
                    if now >= next_sandbox:
                        facts["sandbox"] = workload.sandbox(probes)
                        probes += 1
                        next_sandbox = time.monotonic() + 1800
                    with workload.engine.transaction() as conn:
                        money = conn.execute(
                            "SELECT count(*) AS calls FROM app.usage_calls"
                        ).fetchone()
                        facts["db_time"] = str(
                            conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
                        )
                    if money["calls"] != 0:
                        raise RuntimeError("UNEXPECTED_MODEL_CALL")
                    if source_fingerprint(ROOT) != fingerprint:
                        raise RuntimeError("SOURCE_CHANGED")
                except Exception as error:
                    status = "failed"
                    facts["error_type"] = type(error).__name__
                    facts["error_code"] = (
                        str(error) if isinstance(error, RuntimeError) else "CONTROLLED_FAULT"
                    )
                row = seal_record(
                    {
                        "UTC": datetime.now(UTC).isoformat(),
                        "monotonic": time.monotonic(),
                        "pid": os.getpid(),
                        "boot_id": boot,
                        "source_sha256": fingerprint,
                        "cycles": cycles,
                        "sandbox_probes": probes,
                        "status": status,
                        "facts": facts,
                    },
                    records,
                )
                records.append(row)
                with (segment / "samples.jsonl").open("a") as stream:
                    stream.write(json.dumps(row) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                report = inspect_journal(records)
                save(
                    root / "status.json",
                    {
                        **report,
                        "UTC": row["UTC"],
                        "pid": os.getpid(),
                        "status": "RUNNING" if status == "healthy" else "FAILED",
                        "segment": str(segment),
                        "cycles": cycles,
                        "sandbox_probes": probes,
                        "target_hours": 72,
                        "real_model_calls": 0,
                        "paid_micros": 0,
                    },
                )
                if status != "healthy" or once or report["duration_met"]:
                    break
                stop.wait(60)
        finally:
            workload.close()
            summary = json.loads((root / "status.json").read_text()) if records else {}
            summary.update(
                status="FAILED"
                if records and records[-1]["status"] != "healthy"
                else "STOPPED"
                if stop.is_set() or once
                else "DURATION_REACHED",
                stopped_at=datetime.now(UTC).isoformat(),
            )
            save(root / "status.json", summary)
        return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--crash-after-ingress", action="store_true")
    args = parser.parse_args()
    result = run(args.config, once=args.once, crash=args.crash_after_ingress)
    sys.exit(1 if result.get("status") == "FAILED" else 0)
