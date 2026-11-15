-- SA20 proposal only. Apply as a formal forward migration after exact ownership confirmation.
-- Operational authorization/leases/incidents are facts, not Thread/Ticket/Run or budget copies.
CREATE TABLE app.sa_operations_versions (
    project_id text COLLATE "C" NOT NULL CHECK(length(project_id) BETWEEN 1 AND 200),
    revision bigint NOT NULL CHECK(revision>0),
    definition jsonb NOT NULL CHECK(jsonb_typeof(definition)='object'),
    definition_sha256 char(64) NOT NULL CHECK(definition_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    review_ref text NOT NULL CHECK(length(review_ref) BETWEEN 1 AND 256),
    signed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    valid_until timestamptz NOT NULL,
    PRIMARY KEY(project_id,revision),
    CHECK(valid_until>signed_at AND valid_until<=signed_at+interval '30 days'),
    CHECK(definition->>'schema_version'='source-operations-v1'),
    CHECK(definition - ARRAY['schema_version','enabled','valid_until','components',
        'lease_seconds','source_stale_seconds','backlog_threshold','progress_stale_seconds']='{}'::jsonb),
    CHECK(jsonb_typeof(definition->'enabled')='boolean'),
    CHECK(jsonb_typeof(definition->'components')='array'
        AND jsonb_array_length(definition->'components')<=16)
);
CREATE TABLE app.sa_operations_controls (
    project_id text COLLATE "C" PRIMARY KEY,
    revision bigint NOT NULL CHECK(revision>0),
    FOREIGN KEY(project_id,revision) REFERENCES app.sa_operations_versions(project_id,revision)
);
CREATE TABLE app.sa_operations_components (
    project_id text COLLATE "C" NOT NULL,
    component_id uuid NOT NULL,
    role text NOT NULL CHECK(role IN ('source','scheduler','worker','dispatcher','watchdog')),
    config_revision bigint NOT NULL CHECK(config_revision>0),
    generation bigint NOT NULL CHECK(generation>0),
    boot_id uuid NOT NULL,
    open_request_id uuid NOT NULL,
    sequence bigint NOT NULL DEFAULT 0 CHECK(sequence>=0),
    state text NOT NULL CHECK(state IN ('starting','ready','degraded','stopping','waiting_runner')),
    opened_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    heartbeat_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    tick_request_id uuid,
    tick_sequence bigint NOT NULL DEFAULT 0 CHECK(tick_sequence>=0),
    tick_state text NOT NULL DEFAULT 'idle' CHECK(tick_state IN ('idle','reserved','known','unknown')),
    tick_result jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(tick_result)='object'),
    PRIMARY KEY(project_id,component_id),
    FOREIGN KEY(project_id,config_revision) REFERENCES app.sa_operations_versions(project_id,revision),
    CHECK(expires_at>heartbeat_at AND expires_at<=heartbeat_at+interval '300 seconds'),
    CHECK((tick_state='idle')=(tick_request_id IS NULL)),
    CHECK(tick_result - ARRAY['processed','pending','blocked','unknown','waiting_runner',
        'model_calls','external_writes']='{}'::jsonb)
);
CREATE TABLE app.sa_operations_incidents (
    incident_key char(64) PRIMARY KEY CHECK(incident_key ~ '^[a-f0-9]{64}$'),
    project_id text COLLATE "C" NOT NULL,
    config_revision bigint NOT NULL CHECK(config_revision>0),
    kind text NOT NULL CHECK(kind IN ('component_unavailable','waiting_runner','source_offline',
        'source_permissions','source_gap','input_backlog','semantic_stalled','job_overdue',
        'lease_expired','notification_unknown','usage_unknown','dependency_unavailable')),
    subject_id uuid,
    basis_sha256 char(64) NOT NULL CHECK(basis_sha256 ~ '^[a-f0-9]{64}$'),
    cursor_sha256 char(64) NOT NULL CHECK(cursor_sha256 ~ '^[a-f0-9]{64}$'),
    severity text NOT NULL CHECK(severity IN ('warning','critical')),
    first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
    state text NOT NULL DEFAULT 'open' CHECK(state IN ('open','waiting','escalated')),
    decided_by uuid REFERENCES app.web_accounts(user_id),
    decided_at timestamptz,
    review_ref text CHECK(review_ref IS NULL OR length(review_ref) BETWEEN 1 AND 256),
    FOREIGN KEY(project_id,config_revision) REFERENCES app.sa_operations_versions(project_id,revision),
    CHECK((state='open')=(decided_by IS NULL)),
    CHECK((decided_by IS NULL)=(decided_at IS NULL))
);
CREATE INDEX sa_operations_component_expiry ON app.sa_operations_components(project_id,expires_at);
CREATE INDEX sa_operations_incident_recent ON app.sa_operations_incidents(project_id,first_seen_at DESC);

CREATE FUNCTION app.sa_operations_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN
        RAISE EXCEPTION 'Operations authorization versions are immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_operations_version_immutable BEFORE UPDATE OR DELETE
    ON app.sa_operations_versions FOR EACH ROW EXECUTE FUNCTION app.sa_operations_version_guard();

CREATE FUNCTION app.sa_operations_component_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE version app.sa_operations_versions%ROWTYPE;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Retain component fencing and unknown receipts'; END IF;
    IF TG_OP='UPDATE' THEN
        IF (NEW.project_id,NEW.component_id,NEW.role) IS DISTINCT FROM
            (OLD.project_id,OLD.component_id,OLD.role) THEN
            RAISE EXCEPTION 'Component scope is immutable';
        END IF;
        -- Finishing an already reserved call records truth even after revocation.
        -- No lease, sequence, component scope or work authority is extended.
        IF NEW.tick_sequence NOT IN (OLD.tick_sequence,OLD.tick_sequence+1)
            OR (NEW.tick_request_id IS DISTINCT FROM OLD.tick_request_id AND
                (NEW.tick_state<>'reserved' OR NEW.tick_sequence<>OLD.tick_sequence+1)) THEN
            RAISE EXCEPTION 'Tick sequence is fenced';
        END IF;
        IF OLD.tick_state IN ('reserved','unknown') AND NEW.tick_state IN ('known','unknown')
            AND (NEW.project_id,NEW.component_id,NEW.role,NEW.config_revision,NEW.generation,
                 NEW.boot_id,NEW.open_request_id,NEW.sequence,NEW.state,NEW.opened_at,
                 NEW.heartbeat_at,NEW.expires_at,NEW.tick_request_id,NEW.tick_sequence)
                IS NOT DISTINCT FROM
                (OLD.project_id,OLD.component_id,OLD.role,OLD.config_revision,OLD.generation,
                 OLD.boot_id,OLD.open_request_id,OLD.sequence,OLD.state,OLD.opened_at,
                 OLD.heartbeat_at,OLD.expires_at,OLD.tick_request_id,OLD.tick_sequence) THEN
            RETURN NEW;
        END IF;
    END IF;
    SELECT v.* INTO version FROM app.sa_operations_versions v
        JOIN app.sa_operations_controls c ON c.project_id=v.project_id AND c.revision=v.revision
        WHERE v.project_id=NEW.project_id AND v.revision=NEW.config_revision;
    IF NOT FOUND OR version.valid_until<=clock_timestamp()
        OR version.definition->>'enabled'<>'true'
        OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(version.definition->'components') g
            WHERE g->>'component_id'=NEW.component_id::text AND g->>'role'=NEW.role
                AND g->>'enabled'='true') THEN
        RAISE EXCEPTION 'Current explicit operational component scope required';
    END IF;
    IF NEW.expires_at>version.valid_until THEN RAISE EXCEPTION 'Component lease exceeds authorization'; END IF;
    IF TG_OP='UPDATE' THEN
        IF (NEW.project_id,NEW.component_id,NEW.role) IS DISTINCT FROM
            (OLD.project_id,OLD.component_id,OLD.role) THEN
            RAISE EXCEPTION 'Component scope is immutable';
        END IF;
        IF (NEW.generation=OLD.generation AND
            ((NEW.boot_id,NEW.config_revision,NEW.open_request_id) IS DISTINCT FROM
                (OLD.boot_id,OLD.config_revision,OLD.open_request_id)
             OR NEW.sequence<OLD.sequence))
            OR NEW.generation NOT IN (OLD.generation,OLD.generation+1) THEN
            RAISE EXCEPTION 'Component generation or sequence fenced';
        END IF;
        IF NEW.tick_sequence NOT IN (OLD.tick_sequence,OLD.tick_sequence+1)
            OR (NEW.tick_request_id IS DISTINCT FROM OLD.tick_request_id AND
                (NEW.tick_state<>'reserved' OR NEW.tick_sequence<>OLD.tick_sequence+1)) THEN
            RAISE EXCEPTION 'Tick sequence is fenced';
        END IF;
        IF OLD.tick_state IN ('reserved','unknown') AND
            (NEW.generation<>OLD.generation OR NEW.tick_request_id IS DISTINCT FROM OLD.tick_request_id
             OR NEW.tick_state='idle') THEN
            RAISE EXCEPTION 'Unconfirmed work cannot be reset by reopening a component';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_operations_component_fence BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_operations_components FOR EACH ROW EXECUTE FUNCTION app.sa_operations_component_guard();

CREATE FUNCTION app.sa_operations_incident_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Operational incident evidence is retained'; END IF;
    IF (NEW.incident_key,NEW.project_id,NEW.config_revision,NEW.kind,NEW.subject_id,
        NEW.basis_sha256,NEW.cursor_sha256,NEW.severity,NEW.first_seen_at) IS DISTINCT FROM
       (OLD.incident_key,OLD.project_id,OLD.config_revision,OLD.kind,OLD.subject_id,
        OLD.basis_sha256,OLD.cursor_sha256,OLD.severity,OLD.first_seen_at)
        OR OLD.state<>'open' OR NEW.state='open' OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION 'Only one explicit wait/escalate decision may follow an incident';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_operations_incident_decision BEFORE UPDATE OR DELETE
    ON app.sa_operations_incidents FOR EACH ROW EXECUTE FUNCTION app.sa_operations_incident_guard();
