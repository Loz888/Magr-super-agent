"""Run only after ownership review, formal0055 installation and Bot quiescence.

This changes signed configuration on the original Engine, never usage rows.
It neither installs SQL nor restarts the listener nor issues a model request.
"""

import argparse
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import digest
from mgar_v3.evidence.artifacts import ArtifactStore
from mgar_v3.settings import ROOT, Settings
from mgar_v3.sources.domain import ActorRef
from mgar_v3.sources.ingress_domain import IngressConfigure, IngressSettings
from mgar_v3.sources.registry import SourceRegistry
from mgar_v3.super_agent.budget_domain import BudgetSave
from mgar_v3.super_agent.policy_domain import PolicyDefinition, PolicySave
from mgar_v3.super_agent.source_coding_trial import CodingTrial, authorize_trial
from mgar_v3.super_agent.source_feishu_channel import FeishuNotificationChannel
from mgar_v3.super_agent.source_notification_domain import (
    NotificationConfigure,
    NotificationSettings,
)
from mgar_v3.super_agent.source_notification_ports import NotificationProfile


def atomic(path, value):
    temporary = path.with_name(path.name + ".budget10-new")
    with temporary.open("x") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def ledger(conn):
    # Values are hashed privately; only hashes/counts leave this function.
    result = {}
    for table in ("usage_calls", "router_calls"):
        rows = conn.execute("SELECT * FROM app." + table + " ORDER BY call_id").fetchall()
        result[table] = {
            "rows": len(rows),
            "sha256": hashlib.sha256(
                json.dumps(rows, sort_keys=True, default=str).encode()
            ).hexdigest(),
        }
        assert not any(row["status"] == "reserved" for row in rows), "In-flight model call"
    return result


def main():
    args = argparse.ArgumentParser()
    args.add_argument("--confirmation", required=True, type=Path)
    args.add_argument("--evidence", required=True, type=Path)
    options = args.parse_args()
    confirmation = json.loads(options.confirmation.read_text())
    proposal = Path(__file__).resolve().parent
    sha = hashlib.sha256(
        (proposal / "0055_source_trial_budget_ceiling.sql").read_bytes()
    ).hexdigest()
    assert confirmation["confirmed"] is True and confirmation["SQL_sha256"] == sha
    assert len(confirmation["Harness_commit"]) == 40
    control = ROOT / ".local/feishu-bootstrap"
    pid = int((control / "service.pid").read_text())
    assert not Path(f"/proc/{pid}").exists(), "Quiesce the exact existing Bot first"
    private_path = control / "service-private.json"
    private = json.loads(private_path.read_text())
    assert private["settings"]["project_id"] == "sa22-bot-onboarding"
    settings = Settings(_env_file=None, **private["settings"])
    manifest = json.loads(Path(private["model_manifest_path"]).read_text())
    assistant = json.loads(Path(private["repository_assistant_manifest_path"]).read_text())
    assert manifest["total_lifetime_micros"] == assistant["lifetime_micros"] == 2_000_000
    assert not manifest["publication"] and not manifest["original_branch_writes"]
    now = datetime.now(UTC)
    coding_expiry = datetime.fromisoformat(private["coding_trial_expires_at"])
    assert now < coding_expiry and now < datetime.fromisoformat(private["expires_at"])
    engine = Engine(settings, ArtifactStore(settings.artifact_root))
    options.evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    phases = []

    def phase(name, **details):
        phases.append({"UTC": datetime.now(UTC).isoformat(), "phase": name, **details})
        atomic(options.evidence / ("phase-" + str(len(phases)) + ".json"), phases[-1])

    try:
        binding_bytes = (control / "platform-binding.json").read_bytes()
        assert hashlib.sha256(binding_bytes).hexdigest() == private["scope_binding_sha256"]
        binding = json.loads(binding_bytes)
        instance = SourceRegistry(engine).get_instance(UUID(private["source_id"]))
        profile = NotificationProfile(
            instance.scope,
            instance.revision,
            ActorRef(
                scope=instance.scope,
                id_type="open_id",
                actor_id=binding["bot_open_id"],
                actor_kind="bot",
            ),
            instance.channel_capabilities,
            FeishuNotificationChannel(
                engine, instance.scope, lambda: None, proxy="http://127.0.0.1:7892"
            ),
            mode="live",
        )
        assert profile.profile_sha256 == private["notification_profile_sha256"]
        engine.source_notifications.register_profile(profile)
        with engine.transaction() as conn:
            owner = engine.source_correlations.identities._console(conn, private["cookie"])
            installed = conn.execute(
                "SELECT sha256 FROM app.schema_migrations WHERE name=%s",
                ("0055_source_trial_budget_ceiling.sql",),
            ).fetchone()
            assert installed and installed["sha256"] == sha
            before = ledger(conn)
            assert not conn.execute(
                "SELECT 1 FROM app.sa_scheduler_assignments WHERE state<>'released' LIMIT 1"
            ).fetchone(), "Drain live Worker resources first"
            assert not conn.execute(
                "SELECT 1 FROM app.source_ingress_inputs i "
                "JOIN app.source_ingress_bodies b USING(input_id) WHERE i.project_id=%s "
                "AND i.source_instance_id=%s AND i.policy_revision=%s "
                "AND i.consumed_at IS NULL AND i.unavailable_at IS NULL "
                "AND b.expires_at>clock_timestamp() LIMIT 1",
                (settings.project_id, UUID(private["source_id"]), private["policy_revision"]),
            ).fetchone(), "Finish current admitted source work first"
            trials = conn.execute(
                "SELECT * FROM app.sa_coding_trial_authorizations WHERE project_id=%s "
                "AND source_instance_id=%s AND policy_revision=%s",
                (settings.project_id, UUID(private["source_id"]), private["policy_revision"]),
            ).fetchall()
            assert len(trials) == 1 and trials[0]["maximum_project_micros"] == 2_000_000
            configs = engine.project_budgets._configs(conn)
            head, _ = engine.source_ingress._control(conn, UUID(private["source_id"]))
        original_private = control / (
            "service-private-before-budget10-" + now.strftime("%H%M%S") + ".json"
        )
        assert not original_private.exists()
        shutil.copyfile(private_path, original_private)
        original_private.chmod(0o600)
        for row, definition in configs:
            assert definition.caps.total_micros == 2_000_000
            caps = definition.caps.model_copy(
                update={
                    "total_micros": 10_000_000,
                    "hour_micros": 10_000_000,
                    "day_micros": 10_000_000,
                }
            )
            saved = engine.project_budgets.save(
                private["cookie"],
                row["scope_type"],
                row["scope_id"],
                BudgetSave(
                    expected_revision=row["revision"],
                    definition=definition.model_copy(update={"caps": caps}),
                    review_ref=("Owner authorizes cumulative USD10; "
                                "retain all prior usage and unknown"),
                ),
            )
            phase(
                "original_budget_saved",
                scope_type=row["scope_type"],
                revision=saved["current"]["revision"],
            )
        old_policy = engine.source_policies.read(private["cookie"], UUID(private["policy_id"]))
        definition = old_policy["definition"]
        definition["budget"]["total_limit_micros"] = 10_000_000
        signed = engine.source_policies.save(
            private["cookie"],
            UUID(private["policy_id"]),
            PolicySave(
                expected_revision=private["policy_revision"],
                definition=PolicyDefinition.model_validate(definition),
                review_ref=("Owner USD10 shared cap; unchanged Bot "
                            "identity/repo/tools/expiry/publication"),
            ),
        )
        ingress = engine.source_ingress.configure(
            private["cookie"],
            UUID(private["source_id"]),
            IngressConfigure(
                expected_generation=head["generation"],
                settings=IngressSettings(
                    owner="gateway",
                    policy_id=UUID(private["policy_id"]),
                    policy_revision=signed["current_revision"],
                ),
                review_ref="New revision only; no old-source replay after budget update",
            ),
        )
        current = engine.source_notifications.read(private["cookie"], UUID(private["source_id"]))
        notification = NotificationSettings.model_validate(current["settings"]).model_copy(
            update={
                "policy_revision": signed["current_revision"],
                "policy_sha256": signed["definition_sha256"],
            }
        )
        notices = engine.source_notifications.configure(
            private["cookie"],
            UUID(private["source_id"]),
            NotificationConfigure(
                expected_revision=current["revision"],
                settings=notification,
                review_ref="Same notification destination and limits; bind new signed policy",
            ),
        )
        phase(
            "policy_revision_saved",
            revision=signed["current_revision"],
            generation=ingress["generation"],
            notification_revision=notices["revision"],
        )
        manifest.update(
            total_lifetime_micros=10_000_000,
            created_UTC=now.isoformat(),
            previous_manifest_sha256=private["model_trial_manifest_sha256"],
            budget_review="Owner 2026-11-17 authorizes USD10 cumulative; historical spend retained",
        )
        new_manifest = control / "model-trial-manifest-budget10-20261117.json"
        assert not new_manifest.exists()
        atomic(new_manifest, manifest)
        manifest_sha = hashlib.sha256(new_manifest.read_bytes()).hexdigest()
        assistant.update(
            lifetime_micros=10_000_000,
            created_UTC=now.isoformat(),
            policy_revision=signed["current_revision"],
            model_trial_manifest_sha256=manifest_sha,
            previous_manifest_sha256=private["repository_assistant_manifest_sha256"],
        )
        new_assistant = control / "repository-assistant-budget10-20261117.json"
        assert not new_assistant.exists()
        atomic(new_assistant, assistant)
        trial = {key: trials[0][key] for key in CodingTrial.model_fields}
        trial.update(
            trial_id=uuid4(),
            policy_revision=signed["current_revision"],
            maximum_project_micros=10_000_000,
            manifest_sha256=manifest_sha,
            signed_by=owner,
            review_sha256=digest(["Owner increases cumulative USD10", now.isoformat()]),
            valid_from=now,
            valid_until=coding_expiry,
        )
        authorize_trial(engine, private["cookie"], CodingTrial.model_validate(trial))
        private.update(
            policy_revision=signed["current_revision"],
            generation=ingress["generation"],
            model_manifest_path=str(new_manifest),
            model_trial_manifest_sha256=manifest_sha,
            repository_assistant_manifest_path=str(new_assistant),
            repository_assistant_manifest_sha256=hashlib.sha256(
                new_assistant.read_bytes()
            ).hexdigest(),
        )
        atomic(private_path, private)
        with engine.transaction() as conn:
            after = ledger(conn)
        assert before == after
        phase(
            "completed",
            lifetime_micros=10_000_000,
            original_ledger_hashes_unchanged=True,
            model_calls=0,
            deadline_extended=False,
            Bot_restarted=False,
        )
        print(
            json.dumps(
                {
                    "updated": True,
                    "lifetime_micros": 10_000_000,
                    "original_ledgers_unchanged": True,
                    "model_calls": 0,
                }
            )
        )
    finally:
        engine.close()


if __name__ == "__main__":
    main()
