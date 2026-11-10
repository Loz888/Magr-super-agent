-- SAV1-01: source identity, metadata-only inbox and links to existing business records.
-- No alternate Thread/Ticket/Run lifecycle; no source listener or ACK protocol is enabled.
CREATE TABLE app.source_instances (
    source_instance_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    source_kind text COLLATE "C" NOT NULL CHECK (source_kind ~ '^[a-z][a-z0-9_]{0,31}$'),
    tenant_id text COLLATE "C" NOT NULL CHECK (length(tenant_id) BETWEEN 1 AND 256),
    installation_id text COLLATE "C" NOT NULL CHECK (length(installation_id) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id,source_kind,tenant_id,installation_id),
    UNIQUE (source_instance_id,project_id)
);
CREATE TABLE app.source_instance_versions (
    source_instance_id uuid NOT NULL REFERENCES app.source_instances,
    revision bigint NOT NULL CHECK (revision > 0),
    definition jsonb NOT NULL CHECK (jsonb_typeof(definition)='object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_instance_id,revision),
    CHECK (definition ?& ARRAY['scope','project_id','revision','capabilities','channel_capabilities']),
    CHECK (definition->'scope'->>'source_instance_id'=source_instance_id::text),
    CHECK ((definition->>'revision')::bigint=revision)
);
CREATE TABLE app.source_inbox (
    inbox_id uuid PRIMARY KEY,
    source_instance_id uuid NOT NULL,
    source_revision bigint NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    event_kind text COLLATE "C" NOT NULL,
    remote_event_id text COLLATE "C" NOT NULL,
    entity_id text COLLATE "C" NOT NULL,
    revision_origin text NOT NULL CHECK (revision_origin IN ('native','derived','legacy','unavailable')),
    revision_value text COLLATE "C",
    version_key text NOT NULL CHECK (version_key ~ '^[a-f0-9]{64}$'),
    metadata jsonb NOT NULL CHECK (jsonb_typeof(metadata)='object'),
    occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    FOREIGN KEY (source_instance_id,project_id)
        REFERENCES app.source_instances(source_instance_id,project_id),
    FOREIGN KEY (source_instance_id,source_revision)
        REFERENCES app.source_instance_versions(source_instance_id,revision),
    UNIQUE (inbox_id,project_id),
    UNIQUE (source_instance_id,version_key),
    CHECK ((revision_origin='unavailable')=(revision_value IS NULL)),
    CHECK (revision_value IS NULL OR length(revision_value) BETWEEN 1 AND 256),
    CHECK (metadata ?& ARRAY['schema_version','scope','project_id','source_revision',
        'delivery_id','remote_event_id','entity_id','entity_revision','event_kind',
        'source_thread','occurred_at','received_at','provenance','event_fingerprint']),
    CHECK (metadata->>'schema_version'='source-metadata-v1'),
    CHECK (metadata->'scope'->>'source_instance_id'=source_instance_id::text),
    CHECK (metadata->>'project_id'=project_id),
    CHECK ((metadata->>'source_revision')::bigint=source_revision),
    CHECK (metadata->>'event_kind'=event_kind AND metadata->>'remote_event_id'=remote_event_id),
    CHECK (metadata->>'entity_id'=entity_id),
    CHECK (metadata - ARRAY['schema_version','scope','project_id','source_revision',
        'delivery_id','remote_event_id','entity_id','entity_revision','event_kind',
        'source_thread','occurred_at','received_at','provenance','event_fingerprint']='{}'::jsonb)
);
CREATE UNIQUE INDEX source_inbox_remote_version ON app.source_inbox
    (source_instance_id,event_kind,remote_event_id,entity_id,revision_origin,coalesce(revision_value,''));
CREATE INDEX source_inbox_received ON app.source_inbox(project_id,received_at,inbox_id);

-- Composite keys enforce project and Thread -> Ticket -> Run consistency for source relations.
ALTER TABLE app.threads ADD CONSTRAINT sav1_thread_project UNIQUE (thread_id,project_id);
ALTER TABLE app.tickets ADD CONSTRAINT sav1_ticket_thread UNIQUE (ticket_id,thread_id);
ALTER TABLE app.runs ADD CONSTRAINT sav1_run_ticket UNIQUE (run_id,ticket_id);
ALTER TABLE app.trigger_events ADD CONSTRAINT sav1_trigger_thread UNIQUE (event_id,thread_id);
CREATE TABLE app.source_business_links (
    inbox_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    thread_id uuid NOT NULL,
    trigger_event_id uuid,
    ticket_id uuid,
    run_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (inbox_id,revision),
    FOREIGN KEY (inbox_id,project_id) REFERENCES app.source_inbox(inbox_id,project_id),
    FOREIGN KEY (thread_id,project_id) REFERENCES app.threads(thread_id,project_id),
    FOREIGN KEY (trigger_event_id,thread_id) REFERENCES app.trigger_events(event_id,thread_id),
    FOREIGN KEY (ticket_id,thread_id) REFERENCES app.tickets(ticket_id,thread_id),
    FOREIGN KEY (run_id,ticket_id) REFERENCES app.runs(run_id,ticket_id),
    CHECK (run_id IS NULL OR ticket_id IS NOT NULL)
);
CREATE FUNCTION app.protect_source_contract_fact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'source contract facts are immutable; append a new revision';
END $$;
CREATE TRIGGER immutable_source_identity BEFORE UPDATE ON app.source_instances
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER immutable_source_definition BEFORE UPDATE ON app.source_instance_versions
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER immutable_source_metadata BEFORE UPDATE ON app.source_inbox
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER immutable_source_business_link BEFORE UPDATE ON app.source_business_links
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
