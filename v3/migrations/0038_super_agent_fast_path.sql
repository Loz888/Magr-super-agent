-- SA08 proposed private observer facts. Apply only after migration ownership confirmation.
CREATE TABLE app.sa_fast_path_controls (
    source_instance_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL CHECK (revision>0),
    settings jsonb NOT NULL CHECK (jsonb_typeof(settings)='object'),
    settings_sha256 char(64) NOT NULL CHECK (settings_sha256 ~ '^[a-f0-9]{64}$'),
    updated_by uuid NOT NULL REFERENCES app.web_accounts,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(source_instance_id,project_id) REFERENCES app.source_instances(source_instance_id,project_id),
    UNIQUE(source_instance_id,project_id)
);
CREATE TABLE app.sa_fast_path_versions (
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL CHECK (revision>0),
    settings jsonb NOT NULL CHECK (jsonb_typeof(settings)='object'),
    settings_sha256 char(64) NOT NULL CHECK (settings_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts,
    review_ref text NOT NULL CHECK (length(review_ref) BETWEEN 1 AND 256),
    signed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(source_instance_id,revision),
    FOREIGN KEY(source_instance_id,project_id) REFERENCES app.sa_fast_path_controls(source_instance_id,project_id)
);
CREATE TABLE app.sa_fast_path_origins (
    input_id uuid PRIMARY KEY,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    historical boolean NOT NULL,
    buffered boolean NOT NULL,
    gap boolean NOT NULL,
    old boolean NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id),
    UNIQUE(input_id,source_instance_id,project_id)
);
CREATE TABLE app.sa_fast_path_batches (
    batch_id uuid PRIMARY KEY,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    source_revision bigint NOT NULL CHECK(source_revision>0),
    generation bigint NOT NULL CHECK(generation>0),
    config_revision bigint NOT NULL CHECK(config_revision>0),
    policy_id uuid NOT NULL,
    policy_revision bigint NOT NULL,
    topic_key char(64) NOT NULL CHECK(topic_key ~ '^[a-f0-9]{64}$'),
    purpose_key char(64) NOT NULL CHECK(purpose_key ~ '^[a-f0-9]{64}$'),
    priority integer NOT NULL CHECK(priority IN(0,100)),
    intended_action text NOT NULL CHECK(intended_action IN
        ('would_observe','would_evaluate','would_ask','would_pause','would_cancel')),
    revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
    created_at timestamptz NOT NULL,
    merge_until timestamptz NOT NULL CHECK(merge_until>created_at),
    FOREIGN KEY(source_instance_id,project_id) REFERENCES app.source_instances(source_instance_id,project_id),
    FOREIGN KEY(source_instance_id,config_revision) REFERENCES app.sa_fast_path_versions(source_instance_id,revision),
    FOREIGN KEY(policy_id,policy_revision) REFERENCES app.source_policy_versions(policy_id,revision),
    FOREIGN KEY(policy_id,project_id) REFERENCES app.source_policies(policy_id,project_id),
    UNIQUE(batch_id,source_instance_id,project_id)
);
CREATE TABLE app.sa_fast_path_decisions (
    input_id uuid PRIMARY KEY,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    config_revision bigint NOT NULL CHECK(config_revision>0),
    source_revision bigint NOT NULL CHECK(source_revision>0),
    generation bigint NOT NULL CHECK(generation>0),
    policy_id uuid NOT NULL,
    policy_revision bigint NOT NULL,
    identity_revision bigint NOT NULL CHECK(identity_revision>0),
    disposition text NOT NULL CHECK(disposition IN('ignored','context','candidate','control')),
    reason_code text NOT NULL CHECK(reason_code ~ '^[A-Z0-9_]{1,100}$'),
    mode text NOT NULL CHECK(mode IN('disabled','shadow','auto_investigate','auto_repair')),
    priority integer NOT NULL CHECK(priority IN(0,100)),
    intended_action text NOT NULL CHECK(intended_action IN
        ('would_ignore','would_observe','would_evaluate','would_ask','would_pause','would_cancel')),
    topic_key char(64),
    batch_id uuid,
    observed_at timestamptz NOT NULL,
    FOREIGN KEY(input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id),
    FOREIGN KEY(source_instance_id,config_revision) REFERENCES app.sa_fast_path_versions(source_instance_id,revision),
    FOREIGN KEY(policy_id,policy_revision) REFERENCES app.source_policy_versions(policy_id,revision),
    FOREIGN KEY(policy_id,project_id) REFERENCES app.source_policies(policy_id,project_id),
    FOREIGN KEY(batch_id,source_instance_id,project_id)
        REFERENCES app.sa_fast_path_batches(batch_id,source_instance_id,project_id),
    CHECK((disposition IN('candidate','control')) = (batch_id IS NOT NULL)),
    CHECK((disposition='control') = (priority=100))
);
CREATE INDEX sa_fast_path_batch_members ON app.sa_fast_path_decisions(batch_id);
CREATE INDEX sa_fast_path_merge ON app.sa_fast_path_batches(source_instance_id,purpose_key,created_at DESC);
CREATE INDEX sa_fast_path_decision_time ON app.sa_fast_path_decisions(source_instance_id,observed_at);

CREATE FUNCTION app.sa_fast_path_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'fast path evidence is immutable'; END $$;
CREATE TRIGGER sa_fast_path_versions_immutable BEFORE UPDATE OR DELETE ON app.sa_fast_path_versions
    FOR EACH ROW EXECUTE FUNCTION app.sa_fast_path_immutable();
CREATE TRIGGER sa_fast_path_origins_immutable BEFORE UPDATE OR DELETE ON app.sa_fast_path_origins
    FOR EACH ROW EXECUTE FUNCTION app.sa_fast_path_immutable();
CREATE TRIGGER sa_fast_path_decisions_immutable BEFORE UPDATE OR DELETE ON app.sa_fast_path_decisions
    FOR EACH ROW EXECUTE FUNCTION app.sa_fast_path_immutable();
CREATE FUNCTION app.sa_fast_path_control_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'fast path configuration cannot disappear'; END IF;
    IF NEW.source_instance_id<>OLD.source_instance_id OR NEW.project_id<>OLD.project_id
        OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION 'fast path configuration requires a signed next revision';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_fast_path_control_guard BEFORE UPDATE OR DELETE ON app.sa_fast_path_controls
    FOR EACH ROW EXECUTE FUNCTION app.sa_fast_path_control_guard();
CREATE FUNCTION app.sa_fast_path_signed_head() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM app.sa_fast_path_controls c
        WHERE c.source_instance_id=NEW.source_instance_id AND NOT EXISTS(
            SELECT 1 FROM app.sa_fast_path_versions v WHERE v.source_instance_id=c.source_instance_id
            AND v.revision=c.revision AND v.project_id=c.project_id AND v.settings=c.settings
            AND v.settings_sha256=c.settings_sha256 AND v.signed_by=c.updated_by)) THEN
        RAISE EXCEPTION 'fast path head must match its signed version';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER sa_fast_path_signed_head AFTER INSERT OR UPDATE ON app.sa_fast_path_controls
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.sa_fast_path_signed_head();
CREATE FUNCTION app.sa_fast_path_batch_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'fast path batches cannot disappear'; END IF;
    IF (to_jsonb(NEW)-'revision') IS DISTINCT FROM (to_jsonb(OLD)-'revision')
        OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION 'only batch membership revision may advance';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_fast_path_batch_guard BEFORE UPDATE OR DELETE ON app.sa_fast_path_batches
    FOR EACH ROW EXECUTE FUNCTION app.sa_fast_path_batch_guard();
