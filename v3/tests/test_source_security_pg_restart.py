"""Real PostgreSQL server SIGKILL/restart across original Source commit boundaries."""

import psycopg
import pytest
from sa04_support import envelope, gateway, rows, setup
from sa21_recovery_support import crash_cluster as crash_cluster
from sa21_recovery_support import open_engine
from sa21_support import observation

from mgar_v3.domain import DomainError
from mgar_v3.sources.ingress_crypto import BodyCipher


@pytest.mark.parametrize("point,durable", [("before_commit", False), ("after_commit", True)])
def test_real_pg_server_crash_does_not_ack_uncommitted_or_duplicate_committed_source(
    crash_cluster, point, durable
):
    cluster = crash_cluster
    engine = open_engine(cluster.settings)
    env = setup(engine)
    incoming = envelope(env, body="SA21 durable synthetic source across actual PG restart")
    reached, acks = [], []
    with engine.transaction() as conn:
        before = conn.execute("SELECT pg_postmaster_start_time() AS boot").fetchone()["boot"]

    def checkpoint(name):
        if name == point:
            reached.append(name)
            cluster.crash()

    try:
        try:
            gateway(env, checkpoint=checkpoint).receive(incoming, lambda r: acks.append(r))
        except (DomainError, psycopg.Error):
            pass
        assert reached == [point]
        if not durable:
            assert not acks
        with pytest.raises((DomainError, psycopg.Error)):
            gateway(env).receive(incoming)
    finally:
        engine.close()
        cluster.start()
    recovered = open_engine(cluster.settings)
    try:
        env.engine = recovered
        recovered.source_ingress.cipher = BodyCipher(env.key)
        with recovered.transaction() as conn:
            assert (
                conn.execute("SELECT pg_postmaster_start_time() AS boot").fetchone()["boot"]
                > before
            )
        assert len(rows(env, "inputs")) == int(durable)
        result = gateway(env).receive(incoming, lambda r: acks.append(r))
        assert result.disposition == ("duplicate" if durable else "accepted")
        assert len(rows(env, "inputs")) == len(rows(env, "deliveries")) == 1
        assert recovered.source_ingress.body(env.cookie, result.input_ids[0])
        observation(
            "actual_pg_server_restart",
            point=point,
            durable_before_restart=durable,
            source_inputs=1,
            deliveries=1,
            old_connection_rejected=True,
            actual_owned_PG_ports=cluster.ports,
        )
    finally:
        recovered.close()
