-- SAV1-06: Feishu configuration and minimal transport facts on the sole Engine connection.
CREATE TABLE app.feishu_source_settings (
    source_instance_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision>0),
    source_revision bigint NOT NULL,
    definition jsonb NOT NULL CHECK (jsonb_typeof(definition)='object'
                                    AND definition->>'schema_version'='feishu-bot-v1'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    signed_at timestamptz NOT NULL DEFAULT now(),
    review_ref text NOT NULL,
    FOREIGN KEY(source_instance_id,project_id) REFERENCES app.source_instances(source_instance_id,project_id),
    FOREIGN KEY(source_instance_id,source_revision) REFERENCES app.source_instance_versions,
    UNIQUE(source_instance_id,revision)
);
CREATE TABLE app.feishu_source_versions (
    source_instance_id uuid NOT NULL REFERENCES app.feishu_source_settings,
    revision bigint NOT NULL CHECK(revision>0),
    source_revision bigint NOT NULL,
    definition jsonb NOT NULL,
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    signed_at timestamptz NOT NULL DEFAULT now(),
    review_ref text NOT NULL,
    PRIMARY KEY(source_instance_id,revision)
);
CREATE TABLE app.feishu_source_audit (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_instance_id uuid NOT NULL REFERENCES app.feishu_source_settings,
    event text NOT NULL,
    reason_code text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
-- Transport-only permanent filtering, not another task inbox or business state machine.
-- No platform ID, sender, body, credential or raw envelope is stored.
CREATE TABLE app.feishu_transport_filters (
    receipt_id uuid PRIMARY KEY,
    source_instance_id uuid NOT NULL REFERENCES app.feishu_source_settings,
    event_key text NOT NULL CHECK(event_key ~ '^[a-f0-9]{64}$'),
    payload_sha256 text NOT NULL CHECK(payload_sha256 ~ '^[a-f0-9]{64}$'),
    reason_code text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_instance_id,event_key)
);
CREATE TABLE app.feishu_source_health (
    source_instance_id uuid PRIMARY KEY REFERENCES app.feishu_source_settings,
    config_revision bigint NOT NULL,
    connection text NOT NULL CHECK(connection IN ('unknown','disabled','online','offline','degraded')),
    permissions text NOT NULL CHECK(permissions IN ('unknown','granted','insufficient','revoked')),
    mode text NOT NULL CHECK(mode IN ('controlled','live')),
    reason_code text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    last_event_at timestamptz,
    gap_started_at timestamptz
);
CREATE FUNCTION app.feishu_immutable_fact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Feishu version/filter facts are immutable'; END $$;
CREATE TRIGGER feishu_version_immutable BEFORE UPDATE OR DELETE ON app.feishu_source_versions
FOR EACH ROW EXECUTE FUNCTION app.feishu_immutable_fact();
CREATE TRIGGER feishu_filter_immutable BEFORE UPDATE OR DELETE ON app.feishu_transport_filters
FOR EACH ROW EXECUTE FUNCTION app.feishu_immutable_fact();
