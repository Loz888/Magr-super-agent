-- SA22 proposal only: do not install before exact Harness ownership confirmation.
-- Explicit owner trial authority, not a new Task, financial ledger or quality PASS.
CREATE TABLE app.sa_coding_trial_authorizations (
    trial_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    source_instance_id uuid NOT NULL,
    source_revision bigint NOT NULL CHECK(source_revision>0),
    policy_id uuid NOT NULL,
    policy_revision bigint NOT NULL CHECK(policy_revision>0),
    profile_sha256 char(64) NOT NULL CHECK(profile_sha256 ~ '^[a-f0-9]{64}$'),
    repository text COLLATE "C" NOT NULL CHECK(repository=lower(repository)),
    base_commit char(40) NOT NULL CHECK(base_commit ~ '^[a-f0-9]{40}$'),
    registration_sha256 char(64) NOT NULL CHECK(registration_sha256 ~ '^[a-f0-9]{64}$'),
    h12_commit char(40) NOT NULL CHECK(h12_commit ~ '^[a-f0-9]{40}$'),
    h12_source_sha256 char(64) NOT NULL CHECK(h12_source_sha256 ~ '^[a-f0-9]{64}$'),
    h12_evidence_sha256 char(64) NOT NULL CHECK(h12_evidence_sha256 ~ '^[a-f0-9]{64}$'),
    manifest_sha256 char(64) NOT NULL CHECK(manifest_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    review_sha256 char(64) NOT NULL CHECK(review_sha256 ~ '^[a-f0-9]{64}$'),
    maximum_project_micros bigint NOT NULL CHECK(maximum_project_micros=2000000),
    valid_from timestamptz NOT NULL,
    valid_until timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(source_instance_id,policy_revision,profile_sha256),
    FOREIGN KEY(source_instance_id,project_id)
        REFERENCES app.source_instances(source_instance_id,project_id),
    FOREIGN KEY(policy_id,project_id)
        REFERENCES app.source_policies(policy_id,project_id),
    FOREIGN KEY(policy_id,policy_revision)
        REFERENCES app.source_policy_versions(policy_id,revision),
    CHECK(valid_until>valid_from AND valid_until<=valid_from+interval '1 day')
);
CREATE TRIGGER sa_coding_trial_authorizations_immutable BEFORE UPDATE OR DELETE
    ON app.sa_coding_trial_authorizations FOR EACH ROW
    EXECUTE FUNCTION app.protect_source_contract_fact();
COMMENT ON TABLE app.sa_coding_trial_authorizations IS
    'SA22 explicit owner quality-gate exception for bounded live experiments; H12 FAIL is retained';

CREATE OR REPLACE FUNCTION app.sa_coding_binding_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    scope jsonb;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'coding binding identity is permanent'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 OR NEW.state<>'admitted' THEN
            RAISE EXCEPTION 'coding binding begins admitted at revision one';
        END IF;
        SELECT m.scope INTO scope FROM app.mandates m
            JOIN app.tickets t ON t.mandate_id=m.mandate_id
            JOIN app.runs r ON r.ticket_id=t.ticket_id
            JOIN app.threads h ON h.thread_id=t.thread_id
            WHERE m.mandate_id=NEW.mandate_id AND m.project_id=NEW.project_id
            AND m.actor=NEW.engine_actor AND m.execution_mode=NEW.execution_mode
            AND m.policy_version='source-coding-mandate-v1'
            AND t.ticket_id=NEW.ticket_id AND t.thread_id=NEW.thread_id AND t.status='open'
            AND h.project_id=NEW.project_id AND r.run_id=NEW.run_id
            AND r.graph_version='source-coding-v1' AND r.status='queued'
            AND r.execution_mode=NEW.execution_mode AND r.execution_epoch=0;
        IF scope IS NULL OR scope->>'schema_version' IS DISTINCT FROM 'source-coding-mandate-v1'
            OR scope->>'scope_kind' IS DISTINCT FROM 'source-coding-mandate-v1'
            OR scope->>'coding_id' IS DISTINCT FROM NEW.coding_id::text
            OR scope->>'revision' IS DISTINCT FROM NEW.mandate_revision::text
            OR scope->'target'->>'project_id' IS DISTINCT FROM NEW.project_id
            OR scope->'target'->>'thread_id' IS DISTINCT FROM NEW.thread_id::text
            OR scope->'target'->>'ticket_id' IS DISTINCT FROM NEW.ticket_id::text
            OR scope->'target'->>'run_id' IS DISTINCT FROM NEW.run_id::text
            OR scope->'target'->>'mandate_id' IS DISTINCT FROM NEW.mandate_id::text
            OR scope->'origin'->>'identity_id' IS DISTINCT FROM NEW.identity_id::text
            OR scope->'origin'->>'identity_revision' IS DISTINCT FROM NEW.identity_revision::text
            OR scope->'origin'->>'principal_id' IS DISTINCT FROM NEW.principal_id::text
            OR scope->'origin'->>'engine_actor' IS DISTINCT FROM NEW.engine_actor
            OR scope->'origin'->>'policy_id' IS DISTINCT FROM NEW.policy_id::text
            OR scope->'origin'->>'policy_revision' IS DISTINCT FROM NEW.policy_revision::text
            OR scope->'origin'->>'policy_sha256' IS DISTINCT FROM NEW.policy_sha256
            OR scope->'origin'->'input'->>'input_id' IS DISTINCT FROM NEW.input_id::text
            OR scope->'origin'->'input'->>'source_instance_id'
                IS DISTINCT FROM NEW.source_instance_id::text
            OR scope->'origin'->'input'->>'project_id' IS DISTINCT FROM NEW.project_id
            OR scope->'origin'->'input'->>'version_fingerprint'
                IS DISTINCT FROM NEW.version_fingerprint
            OR scope->'origin'->'input'->>'state_sha256' IS DISTINCT FROM NEW.source_state_sha256
            OR scope->>'worker_profile_sha256' IS DISTINCT FROM NEW.profile_sha256
            OR scope->>'context_sha256' IS DISTINCT FROM NEW.context_sha256
            OR scope->>'acceptance_sha256' IS DISTINCT FROM NEW.acceptance_sha256
            OR scope->>'review_sha256' IS DISTINCT FROM NEW.review_sha256
            OR scope->>'signed_by' IS DISTINCT FROM NEW.signed_by::text THEN
            RAISE EXCEPTION 'coding requires an exact new original typed Mandate; no legacy upgrade';
        END IF;
        IF (scope->>'valid_from')::timestamptz IS DISTINCT FROM NEW.valid_from
            OR (scope->>'valid_until')::timestamptz IS DISTINCT FROM NEW.valid_until
            OR NEW.valid_from>clock_timestamp() OR NEW.valid_until<=clock_timestamp()
            OR NEW.valid_until>(scope->'origin'->>'expires_at')::timestamptz THEN
            RAISE EXCEPTION 'coding validity must fit the frozen current origin';
        END IF;
        IF scope-ARRAY['schema_version','scope_kind','coding_id','revision','target','origin',
            'repository','grant','budget','harness','context_refs','context_sha256',
            'acceptance_sha256','worker_profile_sha256','review_sha256','signed_by',
            'valid_from','valid_until']<>'{}'::jsonb THEN
            RAISE EXCEPTION 'coding scope accepts typed metadata only';
        END IF;
        IF scope->'harness'->'h11'->>'engineering' IS DISTINCT FROM 'PASS'
            OR scope->'harness'->'h11'->>'recovery' IS DISTINCT FROM 'PASS'
            OR scope->'harness'->'h12'->>'engineering' IS DISTINCT FROM 'PASS' THEN
            RAISE EXCEPTION 'coding requires fixed H11 engineering/recovery and H12 engineering gates';
        END IF;
        IF NEW.execution_mode='live' AND (
            scope->'harness'->'h12'->>'live_agent' IS DISTINCT FROM 'PASS'
            OR scope->'harness'->'h12'->>'mvp' IS DISTINCT FROM 'PASS') THEN
            IF NOT EXISTS (
                SELECT 1 FROM app.sa_coding_trial_authorizations trial
                WHERE trial.project_id=NEW.project_id
                AND trial.source_instance_id=NEW.source_instance_id
                AND trial.policy_id=NEW.policy_id AND trial.policy_revision=NEW.policy_revision
                AND trial.profile_sha256=NEW.profile_sha256 AND trial.signed_by=NEW.signed_by
                AND trial.repository=scope->'repository'->>'repository'
                AND trial.base_commit=scope->'repository'->>'base_commit'
                AND trial.registration_sha256=scope->'repository'->>'registration_sha256'
                AND trial.h12_commit=scope->'harness'->'h12'->>'commit'
                AND trial.h12_source_sha256=scope->'harness'->'h12'->>'source_sha256'
                AND trial.h12_evidence_sha256=scope->'harness'->'h12'->>'evidence_sha256'
                AND trial.valid_from<=clock_timestamp() AND trial.valid_until>clock_timestamp()
                AND NEW.valid_until<=trial.valid_until
                AND trial.maximum_project_micros=2000000
            ) THEN
                RAISE EXCEPTION 'live coding requires passed quality gates or explicit bound owner trial authority';
            END IF;
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM app.source_ingress_inputs i
            JOIN app.source_ingress_entities e ON e.entity_key=i.entity_key
            JOIN app.source_ingress_bodies body ON body.input_id=i.input_id
            JOIN app.source_identities identity ON identity.identity_id=i.identity_id
            JOIN app.source_principals principal ON principal.principal_id=identity.principal_id
            JOIN app.source_policies p ON p.policy_id=i.policy_id
            JOIN app.source_policy_versions pv ON pv.policy_id=p.policy_id
                AND pv.revision=p.current_revision
            WHERE i.input_id=NEW.input_id AND i.source_instance_id=NEW.source_instance_id
            AND i.project_id=NEW.project_id AND i.version_fingerprint=NEW.version_fingerprint
            AND i.identity_id=NEW.identity_id AND i.identity_revision=NEW.identity_revision
            AND identity.revision=NEW.identity_revision AND identity.revoked_at IS NULL
            AND principal.principal_id=NEW.principal_id AND principal.engine_actor=NEW.engine_actor
            AND i.policy_id=NEW.policy_id AND i.policy_revision=NEW.policy_revision
            AND p.current_revision=NEW.policy_revision AND NOT pv.revoked
            AND pv.definition_sha256=NEW.policy_sha256
            AND pv.definition->>'mode'='auto_repair'
            AND pv.definition->'receive'->>'enabled'='true'
            AND pv.definition->'receive'->>'model_input'='true'
            AND pv.definition->'admission'->>'enabled'='true'
            AND pv.definition->'execution'->>'enabled'='true'
            AND pv.definition->'execution'->'capabilities'
                @> '["read_repository","edit_files","run_tests"]'::jsonb
            AND (pv.definition->>'valid_from')::timestamptz<=NEW.valid_from
            AND (pv.definition->>'valid_until')::timestamptz>=NEW.valid_until
            AND i.unavailable_at IS NULL AND i.metadata_expires_at>=NEW.valid_until
            AND body.expires_at>=NEW.valid_until
            AND e.head_input_id=i.input_id AND NOT e.ambiguous AND NOT e.deleted
        ) THEN
            RAISE EXCEPTION 'coding requires its exact current canonical identity and policy';
        END IF;
    ELSE
        IF OLD.state='revoked' OR NEW.state<>'revoked' OR NEW.revision<>OLD.revision+1
            OR NEW.updated_at<OLD.updated_at
            OR (to_jsonb(NEW)-ARRAY['revision','state','invalidated_at','reason_code','updated_at'])
                IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['revision','state','invalidated_at','reason_code','updated_at']) THEN
            RAISE EXCEPTION 'coding binding can only be irreversibly revoked with the next revision';
        END IF;
    END IF;
    RETURN NEW;
END $$;
