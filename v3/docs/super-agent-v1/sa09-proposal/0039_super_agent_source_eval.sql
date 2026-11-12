-- SA09 proposal only. Number and shared scope require fixed peer confirmation.
-- Do not install from this directory. Original money and business ledgers stay authoritative.
CREATE TABLE app.sa_source_eval_controls (
    source_instance_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL CHECK (revision>0),
    settings jsonb NOT NULL CHECK (jsonb_typeof(settings)='object'),
    settings_sha256 char(64) NOT NULL CHECK (settings_sha256 ~ '^[a-f0-9]{64}$'),
    updated_by uuid NOT NULL REFERENCES app.web_accounts,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(source_instance_id,project_id)
        REFERENCES app.source_instances(source_instance_id,project_id),
    UNIQUE(source_instance_id,project_id)
);
CREATE TABLE app.sa_source_eval_versions (
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL CHECK (revision>0),
    settings jsonb NOT NULL CHECK (jsonb_typeof(settings)='object'),
    settings_sha256 char(64) NOT NULL CHECK (settings_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts,
    review_ref text NOT NULL CHECK (length(review_ref) BETWEEN 1 AND 256),
    signed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(source_instance_id,revision),
    FOREIGN KEY(source_instance_id,project_id)
        REFERENCES app.sa_source_eval_controls(source_instance_id,project_id),
    UNIQUE(source_instance_id,project_id,revision)
);
CREATE TABLE app.sa_source_eval_snapshots (
    batch_id uuid PRIMARY KEY REFERENCES app.router_batches,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    fast_batch_id uuid NOT NULL,
    fast_batch_revision bigint NOT NULL CHECK (fast_batch_revision>0),
    eval_revision bigint NOT NULL CHECK (eval_revision>0),
    snapshot_sha256 char(64) NOT NULL CHECK (snapshot_sha256 ~ '^[a-f0-9]{64}$'),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(source_instance_id,project_id)
        REFERENCES app.source_instances(source_instance_id,project_id),
    FOREIGN KEY(source_instance_id,project_id,eval_revision)
        REFERENCES app.sa_source_eval_versions(source_instance_id,project_id,revision),
    FOREIGN KEY(fast_batch_id,source_instance_id,project_id)
        REFERENCES app.sa_fast_path_batches(batch_id,source_instance_id,project_id),
    UNIQUE(source_instance_id,fast_batch_id,fast_batch_revision,eval_revision),
    UNIQUE(batch_id,source_instance_id,project_id)
);
CREATE TABLE app.sa_source_eval_inputs (
    batch_id uuid NOT NULL,
    input_id uuid NOT NULL,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    version_fingerprint char(64) NOT NULL CHECK (version_fingerprint ~ '^[a-f0-9]{64}$'),
    PRIMARY KEY(batch_id,input_id),
    FOREIGN KEY(batch_id,source_instance_id,project_id)
        REFERENCES app.sa_source_eval_snapshots(batch_id,source_instance_id,project_id),
    FOREIGN KEY(input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id)
);
CREATE TABLE app.sa_source_eval_results (
    batch_id uuid PRIMARY KEY REFERENCES app.sa_source_eval_snapshots,
    call_id uuid UNIQUE REFERENCES app.router_calls DEFERRABLE INITIALLY DEFERRED,
    outcome_code text NOT NULL CHECK (outcome_code ~ '^[A-Z0-9_]{1,100}$'),
    proposals jsonb CHECK (proposals IS NULL OR jsonb_typeof(proposals)='object'),
    usage_known boolean NOT NULL,
    response_sha256 char(64) CHECK (response_sha256 ~ '^[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE app.sa_source_eval_decisions (
    decision_key char(64) PRIMARY KEY CHECK (decision_key ~ '^[a-f0-9]{64}$'),
    batch_id uuid NOT NULL REFERENCES app.sa_source_eval_snapshots,
    group_id uuid NOT NULL,
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(batch_id,group_id)
);
CREATE INDEX sa_source_eval_snapshot_source
    ON app.sa_source_eval_snapshots(source_instance_id,created_at DESC);
CREATE INDEX sa_source_eval_input_attempts ON app.sa_source_eval_inputs(input_id,batch_id);

CREATE FUNCTION app.sa_source_eval_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'source evaluation evidence is immutable'; END $$;
CREATE TRIGGER sa_source_eval_versions_immutable
    BEFORE UPDATE OR DELETE ON app.sa_source_eval_versions
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_eval_immutable();
CREATE TRIGGER sa_source_eval_snapshots_immutable
    BEFORE UPDATE OR DELETE ON app.sa_source_eval_snapshots
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_eval_immutable();
CREATE TRIGGER sa_source_eval_inputs_immutable
    BEFORE UPDATE OR DELETE ON app.sa_source_eval_inputs
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_eval_immutable();
CREATE TRIGGER sa_source_eval_results_immutable
    BEFORE UPDATE OR DELETE ON app.sa_source_eval_results
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_eval_immutable();
CREATE TRIGGER sa_source_eval_decisions_immutable
    BEFORE UPDATE OR DELETE ON app.sa_source_eval_decisions
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_eval_immutable();

CREATE FUNCTION app.sa_source_eval_control_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'source evaluation configuration cannot disappear';
    END IF;
    IF NEW.source_instance_id<>OLD.source_instance_id OR NEW.project_id<>OLD.project_id
        OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION 'source evaluation configuration requires a signed next revision';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_eval_control_guard
    BEFORE UPDATE OR DELETE ON app.sa_source_eval_controls
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_eval_control_guard();
CREATE FUNCTION app.sa_source_eval_signed_head() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM app.sa_source_eval_controls c
        WHERE c.source_instance_id=NEW.source_instance_id AND NOT EXISTS(
            SELECT 1 FROM app.sa_source_eval_versions v
            WHERE v.source_instance_id=c.source_instance_id AND v.revision=c.revision
            AND v.project_id=c.project_id AND v.settings=c.settings
            AND v.settings_sha256=c.settings_sha256 AND v.signed_by=c.updated_by)) THEN
        RAISE EXCEPTION 'source evaluation head must match its signed version';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER sa_source_eval_signed_head
    AFTER INSERT OR UPDATE ON app.sa_source_eval_controls
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_eval_signed_head();

-- The original batch is only a namespaced financial/worker anchor. Its context_hash
-- binds the Source snapshot below, before the harmless legacy threads=[] wrapper.
CREATE FUNCTION app.sa_source_eval_snapshot_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE original app.router_batches%ROWTYPE;
BEGIN
    SELECT * INTO original FROM app.router_batches WHERE batch_id=NEW.batch_id;
    IF NOT FOUND OR original.project_id IS DISTINCT FROM NEW.project_id
        OR original.context_hash IS DISTINCT FROM NEW.snapshot_sha256::text
        OR jsonb_typeof(original.snapshot) IS DISTINCT FROM 'object'
        OR original.snapshot->>'schema_version' IS DISTINCT FROM 'source-router-snapshot-v1'
        OR original.snapshot->'threads' IS DISTINCT FROM '[]'::jsonb
        OR (original.snapshot-'threads') IS DISTINCT FROM NEW.snapshot THEN
        RAISE EXCEPTION 'source evaluation snapshot must match its original scoped batch';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER sa_source_eval_snapshot_guard
    AFTER INSERT ON app.sa_source_eval_snapshots
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_eval_snapshot_guard();

CREATE FUNCTION app.sa_source_eval_input_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM app.source_ingress_inputs i
        WHERE i.input_id=NEW.input_id AND i.source_instance_id=NEW.source_instance_id
        AND i.project_id=NEW.project_id AND i.version_fingerprint=NEW.version_fingerprint) THEN
        RAISE EXCEPTION 'source evaluation input must match the canonical version';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_eval_input_guard BEFORE INSERT ON app.sa_source_eval_inputs
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_eval_input_guard();

-- A known cost may be settled before or after inserting the sanitized result,
-- provided both facts commit in this same original Engine outer transaction.
-- Unknown calls retain their original router_calls/router_batches facts. They
-- cannot masquerade as a known result by attaching their call_id here.
CREATE FUNCTION app.sa_source_eval_result_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.call_id IS NOT NULL AND NOT EXISTS(
        SELECT 1 FROM app.router_calls c WHERE c.call_id=NEW.call_id
        AND c.batch_id=NEW.batch_id AND c.status='known') THEN
        RAISE EXCEPTION 'source evaluation result requires its original known batch call';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER sa_source_eval_result_guard
    AFTER INSERT ON app.sa_source_eval_results
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_eval_result_guard();
