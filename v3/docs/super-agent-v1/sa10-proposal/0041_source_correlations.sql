-- SA10 PROPOSAL ONLY. 0040 belongs to Harness f554937 section33.
-- 0041 and this exact shared SQL require a new fixed peer acknowledgement.
-- Do not install from docs/. No old table, financial rule or source purge changes.
-- The sole writer is the original Engine and its existing locked connection.
-- Python strict DTOs construct bodyless resource/code/payload objects and their
-- canonical hashes. SQL additionally binds scope, canonical version and original
-- targets. A model/body/URL never supplies any of these authorization facts.

CREATE TABLE app.sa_correlation_resources (
    input_id uuid NOT NULL,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    version_fingerprint char(64) NOT NULL CHECK (version_fingerprint ~ '^[a-f0-9]{64}$'),
    resource_key char(64) NOT NULL CHECK (resource_key ~ '^[a-f0-9]{64}$'),
    resource jsonb NOT NULL CHECK (jsonb_typeof(resource)='object'
        AND octet_length(resource::text)<=8192
        AND resource ?& ARRAY['project_id','scope','repository_id','repository','kind','resource_id']
        AND resource-ARRAY['project_id','scope','repository_id','repository','kind','resource_id']='{}'::jsonb
        AND resource->>'project_id'=project_id
        AND jsonb_typeof(resource->'scope')='object'
        AND resource->'scope' ?& ARRAY['source_kind','source_instance_id','tenant_id','installation_id']
        AND (resource->'scope')-ARRAY['source_kind','source_instance_id','tenant_id','installation_id']='{}'::jsonb
        AND resource->'scope'->>'source_kind'='github'
        AND resource->'scope'->>'source_instance_id'=source_instance_id::text
        AND jsonb_typeof(resource->'repository_id')='number'
        AND resource->>'repository_id' ~ '^[1-9][0-9]{0,18}$'
        AND (resource->>'repository_id')::numeric<=9223372036854775807
        AND resource->>'repository' ~ '^[a-z0-9_.-]+/[a-z0-9_.-]+$'
        AND resource->>'kind' IN ('issue','pull_request','workflow_run','check_run','check_suite')
        AND jsonb_typeof(resource->'resource_id')='string'
        AND resource->>'resource_id' ~ '^[1-9][0-9]{0,18}$'
        AND (resource->>'resource_id')::numeric<=9223372036854775807),
    code jsonb CHECK (code IS NULL OR (jsonb_typeof(code)='object'
        AND octet_length(code::text)<=4096
        AND code ?& ARRAY['resource_key','proof_sha256']
        AND code-ARRAY['resource_key','proof_sha256','head_repository_id','head_sha',
            'base_sha','tree_sha','run_id','run_attempt','check_run_id','check_suite_id']='{}'::jsonb
        AND code->>'resource_key'=resource_key::text
        AND code->>'proof_sha256'=proof_sha256::text)),
    proof_sha256 char(64) NOT NULL CHECK (proof_sha256 ~ '^[a-f0-9]{64}$'),
    code_version_key char(64) CHECK (code_version_key ~ '^[a-f0-9]{64}$'),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (input_id,resource_key),
    UNIQUE (input_id,resource_key,source_instance_id,project_id),
    UNIQUE (input_id,resource_key,source_instance_id,project_id,version_fingerprint),
    FOREIGN KEY (input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id),
    CHECK ((code IS NULL)=(code_version_key IS NULL))
);

-- revision=0 exists only inside the transaction which wins a unique resource
-- claim and invokes a trusted original-connection target factory. It cannot commit.
CREATE TABLE app.sa_task_correlations (
    correlation_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision>=0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (correlation_id,project_id)
);

CREATE TABLE app.sa_task_correlation_versions (
    correlation_id uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision>0),
    project_id text COLLATE "C" NOT NULL,
    previous_revision bigint,
    thread_id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    run_id uuid NOT NULL,
    thread_revision bigint NOT NULL CHECK (thread_revision>=0),
    execution_epoch bigint NOT NULL CHECK (execution_epoch>=0),
    mandate_id uuid NOT NULL REFERENCES app.mandates(mandate_id),
    mandate_sha256 char(64) NOT NULL CHECK (mandate_sha256 ~ '^[a-f0-9]{64}$'),
    repository text COLLATE "C" NOT NULL CHECK (length(repository) BETWEEN 3 AND 512),
    snapshot_sha256 char(64) NOT NULL CHECK (snapshot_sha256 ~ '^[a-f0-9]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'
        AND octet_length(payload::text)<=65536
        AND payload ?& ARRAY['schema_version','target','anchors','state','resource_keys','snapshot_sha256']
        AND payload-ARRAY['schema_version','target','anchors','state','resource_keys','snapshot_sha256']='{}'::jsonb
        AND payload->>'schema_version'='source-correlation-v1'
        AND payload->>'state' IN ('bound','removed')
        AND payload->>'snapshot_sha256'=snapshot_sha256::text
        AND jsonb_typeof(payload->'resource_keys')='array'
        AND jsonb_array_length(payload->'resource_keys') BETWEEN 1 AND 64
        AND jsonb_typeof(payload->'target')='object'
        AND (payload->'target')-ARRAY['ticket_id','run_id','thread_id','thread_revision',
            'execution_epoch','mandate_id','mandate_sha256','project_id','repository','allowed']='{}'::jsonb
        AND jsonb_typeof(payload->'anchors')='array'
        AND jsonb_array_length(payload->'anchors') BETWEEN 1 AND 64),
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    reason_code text COLLATE "C" NOT NULL CHECK (reason_code ~ '^[A-Z0-9_]{1,100}$'),
    review_sha256 char(64) NOT NULL CHECK (review_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    signed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (correlation_id,revision),
    UNIQUE (correlation_id,project_id,revision),
    FOREIGN KEY (correlation_id,project_id)
        REFERENCES app.sa_task_correlations(correlation_id,project_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (correlation_id,previous_revision)
        REFERENCES app.sa_task_correlation_versions(correlation_id,revision)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (thread_id,project_id) REFERENCES app.threads(thread_id,project_id),
    FOREIGN KEY (ticket_id,thread_id) REFERENCES app.tickets(ticket_id,thread_id),
    FOREIGN KEY (run_id,ticket_id) REFERENCES app.runs(run_id,ticket_id),
    CHECK ((revision=1 AND previous_revision IS NULL)
        OR (revision>1 AND previous_revision IS NOT NULL AND previous_revision=revision-1))
);

ALTER TABLE app.sa_task_correlations ADD CONSTRAINT sa_correlation_head_history
    FOREIGN KEY (correlation_id,project_id,revision)
    REFERENCES app.sa_task_correlation_versions(correlation_id,project_id,revision)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE app.sa_task_correlation_claims (
    project_id text COLLATE "C" NOT NULL,
    resource_key char(64) NOT NULL CHECK (resource_key ~ '^[a-f0-9]{64}$'),
    correlation_id uuid NOT NULL,
    original_input_id uuid NOT NULL,
    source_instance_id uuid NOT NULL,
    version_fingerprint char(64) NOT NULL CHECK (version_fingerprint ~ '^[a-f0-9]{64}$'),
    claimed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (project_id,resource_key),
    UNIQUE (project_id,resource_key,correlation_id),
    FOREIGN KEY (correlation_id,project_id)
        REFERENCES app.sa_task_correlations(correlation_id,project_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (original_input_id,resource_key,source_instance_id,project_id,version_fingerprint)
        REFERENCES app.sa_correlation_resources
            (input_id,resource_key,source_instance_id,project_id,version_fingerprint)
        DEFERRABLE INITIALLY DEFERRED
);

-- Each edge names an exact immutable correlation revision. Old edges never
-- silently acquire the new target of a later correction. No FK to source_inbox:
-- retention may purge inbox/body/metadata, retaining canonical IDs and this audit.
-- unit_key partitions multiple problems in one input; a topic is not a claim key.
CREATE TABLE app.sa_thread_link_inputs (
    correlation_id uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision>0),
    input_id uuid NOT NULL,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    version_fingerprint char(64) NOT NULL CHECK (version_fingerprint ~ '^[a-f0-9]{64}$'),
    unit_key char(64) NOT NULL CHECK (unit_key ~ '^[a-f0-9]{64}$'),
    conversation_sha256 char(64) NOT NULL CHECK (conversation_sha256 ~ '^[a-f0-9]{64}$'),
    topic_sha256 char(64) CHECK (topic_sha256 ~ '^[a-f0-9]{64}$'),
    resource_key char(64) CHECK (resource_key ~ '^[a-f0-9]{64}$'),
    linked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (correlation_id,revision,input_id,unit_key),
    FOREIGN KEY (correlation_id,project_id,revision)
        REFERENCES app.sa_task_correlation_versions(correlation_id,project_id,revision)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id),
    FOREIGN KEY (project_id,resource_key,correlation_id)
        REFERENCES app.sa_task_correlation_claims(project_id,resource_key,correlation_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX sa_correlation_input_history ON app.sa_thread_link_inputs
    (project_id,input_id,unit_key,correlation_id,revision);
CREATE INDEX sa_correlation_target_history ON app.sa_task_correlation_versions
    (project_id,ticket_id,correlation_id,revision);
CREATE INDEX sa_correlation_resource_history ON app.sa_correlation_resources
    (project_id,resource_key,input_id);
-- Deliberately no unique index on conversation/topic, title, SHA or Issue number.

CREATE FUNCTION app.sa_correlation_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'source correlation facts are immutable; append an explicit revision';
END $$;
CREATE TRIGGER sa_correlation_resource_immutable BEFORE UPDATE OR DELETE
    ON app.sa_correlation_resources FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_immutable();
CREATE TRIGGER sa_correlation_version_immutable BEFORE UPDATE OR DELETE
    ON app.sa_task_correlation_versions FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_immutable();
CREATE TRIGGER sa_correlation_claim_immutable BEFORE UPDATE OR DELETE
    ON app.sa_task_correlation_claims FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_immutable();
CREATE TRIGGER sa_correlation_link_immutable BEFORE UPDATE OR DELETE
    ON app.sa_thread_link_inputs FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_immutable();

CREATE FUNCTION app.sa_correlation_head_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'source correlation identity cannot disappear';
    ELSIF TG_OP='INSERT' THEN
        IF NEW.revision<>0 THEN
            RAISE EXCEPTION 'source correlation must begin with an uncommitted zero head';
        END IF;
    ELSIF ROW(NEW.correlation_id,NEW.project_id,NEW.created_at)
        IS DISTINCT FROM ROW(OLD.correlation_id,OLD.project_id,OLD.created_at)
        OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION 'source correlation head requires one forward revision without identity change';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_correlation_head_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_task_correlations FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_head_guard();

CREATE FUNCTION app.sa_correlation_canonical_input_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM app.source_ingress_inputs i
        WHERE i.input_id=NEW.input_id AND i.source_instance_id=NEW.source_instance_id
        AND i.project_id=NEW.project_id AND i.version_fingerprint=NEW.version_fingerprint) THEN
        RAISE EXCEPTION 'source correlation input must match its permanent canonical version';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_correlation_resource_canonical BEFORE INSERT ON app.sa_correlation_resources
    FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_canonical_input_guard();
CREATE TRIGGER sa_correlation_link_canonical BEFORE INSERT ON app.sa_thread_link_inputs
    FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_canonical_input_guard();

CREATE FUNCTION app.sa_correlation_resource_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM app.source_ingress_inputs i
        JOIN app.source_ingress_entities e ON e.entity_key=i.entity_key
        JOIN app.source_instances s ON s.source_instance_id=i.source_instance_id
            AND s.project_id=i.project_id
        JOIN app.source_ingress_bodies b ON b.input_id=i.input_id
        WHERE i.input_id=NEW.input_id AND i.source_instance_id=NEW.source_instance_id
        AND i.project_id=NEW.project_id AND i.version_fingerprint=NEW.version_fingerprint
        AND i.operation<>'delete' AND i.unavailable_at IS NULL
        AND i.metadata IS NOT NULL AND i.sender_ref IS NOT NULL
        AND i.metadata_expires_at>clock_timestamp() AND b.expires_at>clock_timestamp()
        AND e.head_input_id=i.input_id AND NOT e.deleted AND NOT e.ambiguous
        AND s.source_kind='github'
        AND s.tenant_id=NEW.resource->'scope'->>'tenant_id'
        AND s.installation_id=NEW.resource->'scope'->>'installation_id') THEN
        RAISE EXCEPTION 'native correlation resource requires a current scoped canonical source input';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_correlation_resource_guard BEFORE INSERT ON app.sa_correlation_resources
    FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_resource_guard();

-- This is an INSERT-time factual snapshot check, not a constraint on future Run
-- epochs/Thread revisions. Later cancellation never rewrites historical targets.
-- Current source/actor/policy authority and canonical digest checks remain the
-- same-transaction service's responsibility, not credentials carried in JSON.
CREATE FUNCTION app.sa_correlation_target_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.payload->'target'->>'thread_id' IS DISTINCT FROM NEW.thread_id::text
        OR NEW.payload->'target'->>'ticket_id' IS DISTINCT FROM NEW.ticket_id::text
        OR NEW.payload->'target'->>'run_id' IS DISTINCT FROM NEW.run_id::text
        OR NEW.payload->'target'->>'project_id' IS DISTINCT FROM NEW.project_id
        OR NEW.payload->'target'->>'repository' IS DISTINCT FROM NEW.repository
        OR NEW.payload->'target'->>'thread_revision' IS DISTINCT FROM NEW.thread_revision::text
        OR NEW.payload->'target'->>'execution_epoch' IS DISTINCT FROM NEW.execution_epoch::text
        OR NEW.payload->'target'->>'mandate_id' IS DISTINCT FROM NEW.mandate_id::text
        OR NEW.payload->'target'->>'mandate_sha256' IS DISTINCT FROM NEW.mandate_sha256::text THEN
        RAISE EXCEPTION 'source correlation payload target must match its original target columns';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM app.threads h
        JOIN app.tickets t ON t.thread_id=h.thread_id
        JOIN app.runs r ON r.ticket_id=t.ticket_id
        JOIN app.mandates m ON m.mandate_id=t.mandate_id
        WHERE h.thread_id=NEW.thread_id AND h.project_id=NEW.project_id
        AND h.revision=NEW.thread_revision AND lower(h.repository)=NEW.repository
        AND t.ticket_id=NEW.ticket_id AND t.mandate_id=NEW.mandate_id
        AND r.run_id=NEW.run_id AND r.execution_epoch=NEW.execution_epoch
        AND m.project_id=NEW.project_id) THEN
        RAISE EXCEPTION 'source correlation target must match the original scoped business records';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_correlation_target_guard BEFORE INSERT ON app.sa_task_correlation_versions
    FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_target_guard();

-- Resolve the final state rather than an earlier NEW.revision=0 trigger image.
-- Claims may precede target creation on the same original connection, but a
-- claim/head/history/link which lacks a complete signed target cannot commit.
CREATE FUNCTION app.sa_correlation_complete_head() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE current_revision bigint;
BEGIN
    SELECT h.revision INTO current_revision FROM app.sa_task_correlations h
        WHERE h.correlation_id=NEW.correlation_id AND h.project_id=NEW.project_id;
    IF NOT FOUND OR current_revision<=0 OR NOT EXISTS (
        SELECT 1 FROM app.sa_task_correlation_versions v
        WHERE v.correlation_id=NEW.correlation_id AND v.project_id=NEW.project_id
        AND v.revision=current_revision) THEN
        RAISE EXCEPTION 'source correlation placeholder requires a complete signed target before commit';
    END IF;
    IF EXISTS (SELECT 1 FROM app.sa_task_correlation_versions v
        WHERE v.correlation_id=NEW.correlation_id AND v.project_id=NEW.project_id
        AND v.revision>current_revision) THEN
        RAISE EXCEPTION 'source correlation history cannot extend beyond its committed head';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM app.sa_task_correlation_claims c
        WHERE c.correlation_id=NEW.correlation_id AND c.project_id=NEW.project_id) THEN
        RAISE EXCEPTION 'source correlation requires an exact native resource claim';
    END IF;
    IF EXISTS (SELECT 1 FROM app.sa_task_correlation_versions v
        WHERE v.correlation_id=NEW.correlation_id AND v.project_id=NEW.project_id
        AND NOT EXISTS (SELECT 1 FROM app.sa_thread_link_inputs l
            WHERE l.correlation_id=v.correlation_id AND l.project_id=v.project_id
            AND l.revision=v.revision)) THEN
        RAISE EXCEPTION 'every source correlation revision requires a canonical input anchor';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER sa_correlation_head_complete AFTER INSERT OR UPDATE
    ON app.sa_task_correlations DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_complete_head();
CREATE CONSTRAINT TRIGGER sa_correlation_version_complete AFTER INSERT
    ON app.sa_task_correlation_versions DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_complete_head();
CREATE CONSTRAINT TRIGGER sa_correlation_claim_complete AFTER INSERT
    ON app.sa_task_correlation_claims DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_complete_head();
CREATE CONSTRAINT TRIGGER sa_correlation_link_complete AFTER INSERT
    ON app.sa_thread_link_inputs DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.sa_correlation_complete_head();
