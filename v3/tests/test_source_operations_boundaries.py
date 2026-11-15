"""New formal liveness-history and old-process observation boundaries."""

from uuid import UUID, uuid4

import httpx
from sa20_process_support import listener
from sa20_support import configured, opened

from mgar_v3.api import create_app
from mgar_v3.super_agent.source_operations_client import LocalOperationsClient
from mgar_v3.super_agent.source_operations_domain import (
    ComponentGrant,
    ComponentOpen,
    OperationsSave,
    ServiceClient,
    SignalSubmission,
    signals,
)


def test_current_component_survives_more_than_sixteen_retained_old_generations(engine, tmp_path):
    env = configured(engine, tmp_path)
    for index in range(1, 19):
        component = UUID(int=index)
        definition = env.definition.model_copy(
            update={
                "components": (ComponentGrant(component_id=component, role="worker", enabled=True),)
            }
        )
        env.service.save(
            env.cookie,
            OperationsSave(
                expected_revision=index, definition=definition, review_ref="replace-component-scope"
            ),
        )
        env.service.open(
            component,
            ComponentOpen(
                config_revision=index + 1,
                expected_generation=0,
                boot_id=uuid4(),
                request_id=uuid4(),
            ),
        )
    view = env.service.service_view(component)
    assert (
        len(view.snapshot.components) == 1 and view.snapshot.components[0].component_id == component
    )
    with engine.transaction() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM app.sa_operations_components").fetchone()["n"]
            == 18
        )


def test_real_watchdog_observation_requires_current_boot_generation_and_cannot_use_old_lease(
    engine, tmp_path, trigger
):
    env = configured(engine, tmp_path)
    watchdog = opened(env, "watchdog")
    engine.ingest(trigger)
    with listener(create_app(env.settings, engine=engine, run_background=False)) as url:
        view = env.service.service_view(watchdog.id)
        fact = next(f for f in signals(view.snapshot, env.definition) if f.kind == "waiting_runner")
        command = SignalSubmission(
            kind=fact.kind,
            subject_id=fact.subject_id,
            observation_key=fact.key(env.service.project),
            snapshot_sha256=view.snapshot_sha256,
        )
        endpoint = f"/api/v3/source-operations/components/{watchdog.id}/observations"
        headers = {"X-MGAR-Operations-Token": env.tokens["watchdog"]}
        with httpx.Client(base_url=url, trust_env=False) as remote:
            assert (
                remote.post(
                    endpoint, json=command.model_dump(mode="json"), headers=headers
                ).status_code
                == 409
            )
            headers.update(
                {"X-MGAR-Operations-Boot": str(watchdog.boot), "X-MGAR-Operations-Generation": "1"}
            )
            assert (
                remote.post(
                    endpoint, json=command.model_dump(mode="json"), headers=headers
                ).status_code
                == 200
            )
            env.service.open(
                watchdog.id,
                ComponentOpen(
                    config_revision=1, expected_generation=1, boot_id=uuid4(), request_id=uuid4()
                ),
            )
            current = env.service.service_view(watchdog.id)
            command = command.model_copy(update={"snapshot_sha256": current.snapshot_sha256})
            assert (
                remote.post(
                    endpoint, json=command.model_dump(mode="json"), headers=headers
                ).status_code
                == 409
            )


def test_native_watchdog_client_sends_lease_headers_and_saves_only_recomputed_incidents(
    engine, tmp_path, trigger
):
    env = configured(engine, tmp_path)
    engine.ingest(trigger)
    with listener(create_app(env.settings, engine=engine, run_background=False)) as url:
        client = LocalOperationsClient(
            ServiceClient(
                api_url=url,
                component_id=env.ids["watchdog"],
                role="watchdog",
                token=env.tokens["watchdog"],
            )
        )
        try:
            result = client.poll()
            assert result["status"] == "observed" and result["signals"] > 0
            items = env.service.incidents(env.cookie)["items"]
            assert any(i["kind"] == "waiting_runner" for i in items)
            assert all(i["state"] == "open" for i in items)
        finally:
            client.close()
        with engine.transaction() as conn:
            assert not conn.execute("SELECT 1 FROM app.usage_calls").fetchone()
