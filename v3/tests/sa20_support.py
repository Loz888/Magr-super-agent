"""Independent SA20 local state, original Owner/Engine and private fake credentials."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from sa19_support import owner

from mgar_v3.super_agent.source_operations_domain import (
    ComponentGrant,
    ComponentOpen,
    OperationsDefinition,
    OperationsSave,
)


def configured(
    engine,
    tmp_path,
    roles=("scheduler", "worker", "dispatcher", "watchdog"),
    *,
    owner_env=None,
    source_ids=(),
    lease_seconds=30,
):
    if owner_env is None:
        env = owner(engine)
    else:
        from mgar_v3.web_access import WebAccounts

        accounts = WebAccounts(engine)
        user = accounts.session(owner_env.cookie)
        assert user is not None
        env = SimpleNamespace(
            engine=engine, cookie=owner_env.cookie, accounts=accounts, user_id=user.user_id
        )
    env.service = engine.source_operations
    env.ids = {role: uuid4() for role in roles}
    env.tokens = {role: ("sa20_" + uuid4().hex + uuid4().hex) for role in roles}
    env.definition = OperationsDefinition(
        enabled=True,
        lease_seconds=lease_seconds,
        valid_until=datetime.now(UTC) + timedelta(hours=1),
        components=tuple(
            ComponentGrant(
                component_id=c,
                role=role,
                enabled=True,
                source_ids=source_ids if role == "source" else (),
            )
            for role, c in env.ids.items()
        ),
    )
    env.service.save(
        env.cookie,
        OperationsSave(
            expected_revision=0,
            definition=env.definition,
            review_ref="SA20-local-engineering-only",
        ),
    )
    env.secrets_path = tmp_path / "operations-secrets.json"
    env.secrets_path.write_text(
        json.dumps(
            {
                "components": [
                    {"component_id": str(c), "token": env.tokens[r]} for r, c in env.ids.items()
                ]
            }
        )
    )
    env.secrets_path.chmod(0o600)
    env.settings = engine.settings.model_copy(
        update={"operations_service_config": env.secrets_path}
    )
    return env


def opened(env, role="worker"):
    component_id, boot = env.ids[role], uuid4()
    request = ComponentOpen(
        config_revision=1, expected_generation=0, boot_id=boot, request_id=uuid4()
    )
    row = env.service.open(component_id, request)
    return SimpleNamespace(
        id=component_id, boot=boot, generation=row["generation"], request=request
    )


def disable(env):
    current = env.service.read(env.cookie)
    return env.service.save(
        env.cookie,
        OperationsSave(
            expected_revision=current["revision"],
            definition=env.definition.model_copy(update={"enabled": False}),
            review_ref="SA20-disable",
        ),
    )
