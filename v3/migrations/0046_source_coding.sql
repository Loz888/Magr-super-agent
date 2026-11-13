-- SA13 REVIEW DRAFT ONLY. 0046 is proposed, not allocated or installed.
-- 0045 belongs to Harness. Do not copy this file to migrations before agreement.
-- Original Engine/held connection, Mandate, Run and usage_calls remain authority.
-- No listener, provider, worker, payment or publication is activated by this DDL.

CREATE TABLE app.sa_coding_bindings (
    coding_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL CHECK (length(project_id) BETWEEN 1 AND 200),
    thread_id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    run_id uuid NOT NULL UNIQUE,
    mandate_id uuid NOT NULL REFERENCES app.mandates(mandate_id),
    mandate_revision bigint NOT NULL CHECK (mandate_revision>0),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    request_id uuid NOT NULL,
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    execution_mode text NOT NULL CHECK (execution_mode IN ('fixture','live')),
    source_instance_id uuid NOT NULL,
    input_id uuid NOT NULL,
    version_fingerprint char(64) NOT NULL CHECK (version_fingerprint ~ '^[a-f0-9]{64}$'),
    source_state_sha256 char(64) NOT NULL CHECK (source_state_sha256 ~ '^[a-f0-9]{64}$'),
    identity_id uuid NOT NULL,
    identity_revision bigint NOT NULL CHECK (identity_revision>0),
    principal_id uuid NOT NULL,
    engine_actor text COLLATE "C" NOT NULL CHECK (engine_actor ~ '^sa_[a-f0-9]{32}$'),
    policy_id uuid NOT NULL,
    policy_revision bigint NOT NULL CHECK (policy_revision>0),
    policy_sha256 char(64) NOT NULL CHECK (policy_sha256 ~ '^[a-f0-9]{64}$'),
    mandate_sha256 char(64) NOT NULL CHECK (mandate_sha256 ~ '^[a-f0-9]{64}$'),
    profile_sha256 char(64) NOT NULL CHECK (profile_sha256 ~ '^[a-f0-9]{64}$'),
    context_sha256 char(64) NOT NULL CHECK (context_sha256 ~ '^[a-f0-9]{64}$'),
    acceptance_sha256 char(64) NOT NULL CHECK (acceptance_sha256 ~ '^[a-f0-9]{64}$'),
    review_sha256 char(64) NOT NULL CHECK (review_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    valid_from timestamptz NOT NULL,
    valid_until timestamptz NOT NULL CHECK (valid_until>valid_from),
    state text NOT NULL DEFAULT 'admitted' CHECK (state IN ('admitted','revoked')),
    invalidated_at timestamptz,
    reason_code text CHECK (reason_code ~ '^[A-Z0-9_]{1,100}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(coding_id,project_id),
    UNIQUE(coding_id,project_id,run_id),
    UNIQUE(project_id,request_id),
    FOREIGN KEY(thread_id,project_id) REFERENCES app.threads(thread_id,project_id),
    FOREIGN KEY(ticket_id,thread_id) REFERENCES app.tickets(ticket_id,thread_id),
    FOREIGN KEY(run_id,ticket_id) REFERENCES app.runs(run_id,ticket_id),
    FOREIGN KEY(input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id),
    FOREIGN KEY(identity_id,source_instance_id,project_id)
        REFERENCES app.source_identities(identity_id,source_instance_id,project_id),
    FOREIGN KEY(principal_id,project_id)
        REFERENCES app.source_principals(principal_id,project_id),
    FOREIGN KEY(policy_id,project_id) REFERENCES app.source_policies(policy_id,project_id),
    FOREIGN KEY(policy_id,policy_revision)
        REFERENCES app.source_policy_versions(policy_id,revision),
    CHECK (updated_at>=created_at),
    CHECK ((state='admitted' AND invalidated_at IS NULL AND reason_code IS NULL)
        OR (state='revoked' AND invalidated_at IS NOT NULL AND reason_code IS NOT NULL
            AND invalidated_at>=created_at))
);

CREATE TABLE app.sa_coding_attempts (
    attempt_id uuid PRIMARY KEY,
    coding_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    run_id uuid NOT NULL,
    assignment_id uuid NOT NULL UNIQUE,
    session_id uuid NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch>0),
    workspace_id uuid NOT NULL,
    workspace_sha256 char(64) NOT NULL CHECK (workspace_sha256 ~ '^[a-f0-9]{64}$'),
    profile_sha256 char(64) NOT NULL CHECK (profile_sha256 ~ '^[a-f0-9]{64}$'),
    initial_tree_sha256 char(64) NOT NULL CHECK (initial_tree_sha256 ~ '^[a-f0-9]{64}$'),
    environment_sha256 char(64) NOT NULL CHECK (environment_sha256 ~ '^[a-f0-9]{64}$'),
    request_id uuid NOT NULL,
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    state text NOT NULL DEFAULT 'ready'
        CHECK (state IN ('ready','running','stopped','result_recorded')),
    fenced_at timestamptz,
    reason_code text CHECK (reason_code ~ '^[A-Z0-9_]{1,100}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(attempt_id,project_id),
    UNIQUE(attempt_id,coding_id,project_id,run_id,assignment_id,session_id,execution_epoch),
    UNIQUE(project_id,request_id),
    FOREIGN KEY(coding_id,project_id,run_id)
        REFERENCES app.sa_coding_bindings(coding_id,project_id,run_id),
    FOREIGN KEY(assignment_id,project_id,session_id,run_id,execution_epoch)
        REFERENCES app.sa_scheduler_assignments
            (assignment_id,project_id,session_id,run_id,execution_epoch),
    CHECK (updated_at>=created_at AND (fenced_at IS NULL OR fenced_at>=created_at)),
    CHECK ((state IN ('ready','running','result_recorded') AND fenced_at IS NULL)
        OR (state='stopped' AND fenced_at IS NOT NULL AND reason_code IS NOT NULL))
);
CREATE UNIQUE INDEX sa_coding_one_open_attempt ON app.sa_coding_attempts(coding_id)
    WHERE state IN ('ready','running');

CREATE TABLE app.sa_coding_operations (
    operation_id uuid PRIMARY KEY,
    attempt_id uuid NOT NULL,
    coding_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    run_id uuid NOT NULL,
    assignment_id uuid NOT NULL,
    session_id uuid NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch>0),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    kind text NOT NULL CHECK (kind IN ('model','tool','verification')),
    capability text NOT NULL CHECK (capability IN ('model','read','write','execute','verify')),
    step_key text COLLATE "C" NOT NULL CHECK (step_key ~ '^[A-Za-z0-9_.:-]{1,200}$'),
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    context_sha256 char(64) NOT NULL CHECK (context_sha256 ~ '^[a-f0-9]{64}$'),
    tree_sha256 char(64) NOT NULL CHECK (tree_sha256 ~ '^[a-f0-9]{64}$'),
    environment_sha256 char(64) NOT NULL CHECK (environment_sha256 ~ '^[a-f0-9]{64}$'),
    usage_call_id uuid UNIQUE REFERENCES app.usage_calls(call_id),
    resource_operation_id uuid UNIQUE,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','running','unknown','known')),
    result_code text CHECK (result_code IN ('ok','failed','denied','cancelled','absent')),
    result_sha256 char(64) CHECK (result_sha256 ~ '^[a-f0-9]{64}$'),
    observed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(operation_id,project_id),
    UNIQUE(operation_id,attempt_id,coding_id,project_id),
    UNIQUE(attempt_id,kind,step_key),
    FOREIGN KEY(attempt_id,coding_id,project_id,run_id,assignment_id,session_id,execution_epoch)
        REFERENCES app.sa_coding_attempts
            (attempt_id,coding_id,project_id,run_id,assignment_id,session_id,execution_epoch),
    FOREIGN KEY(resource_operation_id,assignment_id,project_id)
        REFERENCES app.sa_scheduler_operations(operation_id,assignment_id,project_id),
    CHECK ((kind='model' AND capability='model' AND usage_call_id IS NOT NULL
            AND resource_operation_id IS NULL)
        OR (kind='tool' AND capability IN ('read','write') AND usage_call_id IS NULL
            AND resource_operation_id IS NULL)
        OR (kind='tool' AND capability='execute' AND usage_call_id IS NULL
            AND resource_operation_id IS NOT NULL)
        OR (kind='verification' AND capability='verify' AND usage_call_id IS NULL
            AND resource_operation_id IS NULL)),
    CHECK (updated_at>=created_at AND (observed_at IS NULL OR observed_at>=created_at)),
    CHECK ((state='known' AND result_code IS NOT NULL AND result_sha256 IS NOT NULL
            AND observed_at IS NOT NULL)
        OR (state<>'known' AND result_code IS NULL AND result_sha256 IS NULL
            AND observed_at IS NULL))
);
CREATE INDEX sa_coding_pending_operations ON app.sa_coding_operations(assignment_id,state)
    WHERE state<>'known';

CREATE TABLE app.sa_coding_payloads (
    payload_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    coding_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    payload_kind text NOT NULL CHECK (payload_kind IN ('model_response','checkpoint')),
    ciphertext bytea,
    ciphertext_sha256 char(64) NOT NULL CHECK (ciphertext_sha256 ~ '^[a-f0-9]{64}$'),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
    plaintext_bytes integer NOT NULL CHECK (plaintext_bytes BETWEEN 1 AND 1048576),
    context_sha256 char(64) NOT NULL CHECK (context_sha256 ~ '^[a-f0-9]{64}$'),
    expires_at timestamptz NOT NULL,
    unavailable_at timestamptz,
    reason_code text CHECK (reason_code ~ '^[A-Z0-9_]{1,100}$'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(operation_id,payload_kind),
    FOREIGN KEY(operation_id,attempt_id,coding_id,project_id)
        REFERENCES app.sa_coding_operations(operation_id,attempt_id,coding_id,project_id),
    CHECK (expires_at>created_at AND expires_at<=created_at+interval '1 hour'),
    CHECK (updated_at>=created_at),
    CHECK ((ciphertext IS NOT NULL AND unavailable_at IS NULL AND reason_code IS NULL
            AND octet_length(ciphertext) BETWEEN 1 AND 1600000)
        OR (ciphertext IS NULL AND unavailable_at IS NOT NULL AND reason_code IS NOT NULL
            AND unavailable_at>=created_at))
);
CREATE INDEX sa_coding_payload_expiry ON app.sa_coding_payloads(expires_at)
    WHERE unavailable_at IS NULL;

CREATE TABLE app.sa_coding_verifications (
    verification_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL UNIQUE,
    execution_operation_id uuid,
    attempt_id uuid NOT NULL,
    coding_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    operation_sha256 char(64) NOT NULL CHECK (operation_sha256 ~ '^[a-f0-9]{64}$'),
    mandate_sha256 char(64) NOT NULL CHECK (mandate_sha256 ~ '^[a-f0-9]{64}$'),
    receipt_sha256 char(64) NOT NULL CHECK (receipt_sha256 ~ '^[a-f0-9]{64}$'),
    tree_sha256 char(64) NOT NULL CHECK (tree_sha256 ~ '^[a-f0-9]{64}$'),
    environment_sha256 char(64) NOT NULL CHECK (environment_sha256 ~ '^[a-f0-9]{64}$'),
    profile_sha256 char(64) NOT NULL CHECK (profile_sha256 ~ '^[a-f0-9]{64}$'),
    acceptance_sha256 char(64) NOT NULL CHECK (acceptance_sha256 ~ '^[a-f0-9]{64}$'),
    provenance_sha256 char(64) NOT NULL CHECK (provenance_sha256 ~ '^[a-f0-9]{64}$'),
    result text NOT NULL CHECK (result IN ('passed','failed','incomplete')),
    tests_run integer NOT NULL DEFAULT 0 CHECK (tests_run BETWEEN 0 AND 1000000),
    tests_failed integer NOT NULL DEFAULT 0 CHECK (tests_failed BETWEEN 0 AND tests_run),
    observed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(operation_id,attempt_id,coding_id,project_id)
        REFERENCES app.sa_coding_operations(operation_id,attempt_id,coding_id,project_id),
    FOREIGN KEY(execution_operation_id,attempt_id,coding_id,project_id)
        REFERENCES app.sa_coding_operations(operation_id,attempt_id,coding_id,project_id),
    CHECK (observed_at<=created_at),
    CHECK (result<>'passed' OR
        (execution_operation_id IS NOT NULL AND tests_run>0 AND tests_failed=0))
);

CREATE TABLE app.sa_coding_events (
    event_id bigserial PRIMARY KEY,
    coding_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    attempt_id uuid,
    operation_id uuid,
    revision bigint NOT NULL CHECK (revision>0),
    event_type text NOT NULL CHECK (event_type IN (
        'CODING_ADMITTED','CODING_REVOKED','ATTEMPT_READY','ATTEMPT_RUNNING',
        'ATTEMPT_STOPPED','ATTEMPT_RESULT_RECORDED','OPERATION_QUEUED',
        'OPERATION_RUNNING','OPERATION_UNKNOWN','OPERATION_KNOWN',
        'PAYLOAD_STORED','PAYLOAD_ERASED','VERIFICATION_RECORDED')),
    reason_code text CHECK (reason_code ~ '^[A-Z0-9_]{1,100}$'),
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(coding_id,project_id) REFERENCES app.sa_coding_bindings(coding_id,project_id),
    FOREIGN KEY(attempt_id,project_id) REFERENCES app.sa_coding_attempts(attempt_id,project_id),
    FOREIGN KEY(operation_id,project_id)
        REFERENCES app.sa_coding_operations(operation_id,project_id),
    CHECK (jsonb_typeof(details)='object' AND octet_length(details::text)<=4096
        AND details-ARRAY['state','previous_state','epoch','request_sha256','result_sha256',
            'context_sha256','tree_sha256','profile_sha256','receipt_sha256',
            'verification_result','tests_run','tests_failed','payload_kind']='{}'::jsonb)
);
CREATE INDEX sa_coding_event_history ON app.sa_coding_events(coding_id,event_id);

-- This checks fresh relational fences, not the entire signed Source policy.
-- The Engine must additionally run the typed Source/role/path/budget validators
-- and final IO guard on this same held connection; SQL data is not a capability.
CREATE FUNCTION app.sa_coding_current(
    coding uuid, run_target uuid, assigned uuid, worker uuid, epoch bigint
) RETURNS boolean LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS (
        SELECT 1 FROM app.sa_coding_bindings b
        JOIN app.runs r ON r.run_id=b.run_id
        JOIN app.tickets t ON t.ticket_id=r.ticket_id
        JOIN app.mandates m ON m.mandate_id=t.mandate_id
        JOIN app.sa_scheduler_assignments a ON a.assignment_id=assigned
        JOIN app.sa_scheduler_sessions s ON s.session_id=a.session_id
        JOIN app.sa_scheduler_configs ch ON ch.project_id=a.project_id
        JOIN app.sa_scheduler_config_versions cv
            ON cv.project_id=ch.project_id AND cv.revision=ch.revision
        JOIN app.source_ingress_inputs i ON i.input_id=b.input_id
        JOIN app.source_ingress_entities e ON e.entity_key=i.entity_key
        JOIN app.source_ingress_bodies body ON body.input_id=i.input_id
        JOIN app.source_identities identity ON identity.identity_id=b.identity_id
        JOIN app.source_principals principal ON principal.principal_id=b.principal_id
        JOIN app.source_policies p ON p.policy_id=b.policy_id
        JOIN app.source_policy_versions pv ON pv.policy_id=p.policy_id
            AND pv.revision=p.current_revision
        WHERE b.coding_id=coding AND b.run_id=run_target AND b.state='admitted'
        AND b.valid_from<=clock_timestamp() AND b.valid_until>clock_timestamp()
        AND r.status='running' AND r.execution_epoch=epoch AND t.status='open'
        AND t.mandate_id=b.mandate_id AND m.policy_version='source-coding-mandate-v1'
        AND a.run_id=b.run_id AND a.project_id=b.project_id
        AND a.mandate_id=b.mandate_id AND a.session_id=worker
        AND a.execution_epoch=epoch AND a.state='active' AND a.lease_until>clock_timestamp()
        AND s.project_id=b.project_id AND s.state='registered'
        AND s.expires_at>clock_timestamp() AND s.profile_sha256=b.profile_sha256
        AND ch.revision=a.config_revision AND cv.enabled
        AND cv.valid_from<=clock_timestamp() AND cv.valid_until>clock_timestamp()
        AND i.source_instance_id=b.source_instance_id AND i.project_id=b.project_id
        AND i.version_fingerprint=b.version_fingerprint AND i.identity_id=b.identity_id
        AND i.identity_revision=b.identity_revision AND i.policy_id=b.policy_id
        AND i.policy_revision=b.policy_revision AND i.unavailable_at IS NULL
        AND i.metadata_expires_at>clock_timestamp() AND body.expires_at>clock_timestamp()
        AND e.head_input_id=i.input_id AND NOT e.ambiguous AND NOT e.deleted
        AND identity.revision=b.identity_revision AND identity.revoked_at IS NULL
        AND identity.principal_id=b.principal_id AND identity.project_id=b.project_id
        AND principal.project_id=b.project_id AND principal.engine_actor=b.engine_actor
        AND p.project_id=b.project_id AND p.current_revision=b.policy_revision
        AND NOT pv.revoked AND pv.definition_sha256=b.policy_sha256
        AND pv.definition->>'mode'='auto_repair'
        AND pv.definition->'receive'->>'enabled'='true'
        AND pv.definition->'receive'->>'model_input'='true'
        AND pv.definition->'admission'->>'enabled'='true'
        AND pv.definition->'execution'->>'enabled'='true'
        AND pv.definition->'execution'->'capabilities'
            @> '["read_repository","edit_files","run_tests"]'::jsonb
        AND (pv.definition->>'valid_from')::timestamptz<=clock_timestamp()
        AND (pv.definition->>'valid_until')::timestamptz>clock_timestamp()
    );
$$;

CREATE FUNCTION app.sa_coding_binding_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
            RAISE EXCEPTION 'engineering or model connectivity cannot grant live coding';
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
CREATE TRIGGER sa_coding_binding_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_coding_bindings FOR EACH ROW EXECUTE FUNCTION app.sa_coding_binding_guard();

CREATE FUNCTION app.sa_coding_attempt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'coding attempt identity is permanent'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 OR NEW.state<>'ready' OR NOT app.sa_coding_current(
            NEW.coding_id,NEW.run_id,NEW.assignment_id,NEW.session_id,NEW.execution_epoch) THEN
            RAISE EXCEPTION 'coding attempt requires its current active original assignment';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM app.sa_scheduler_assignments a
            JOIN app.sa_coding_bindings b ON b.coding_id=NEW.coding_id
            JOIN app.mandates m ON m.mandate_id=b.mandate_id
            WHERE a.assignment_id=NEW.assignment_id AND a.workspace_id=NEW.workspace_id
            AND a.workspace_sha256=NEW.workspace_sha256
            AND b.profile_sha256=NEW.profile_sha256
            AND m.scope->'repository'->>'materialized_tree_sha256'=NEW.initial_tree_sha256
            AND m.scope->'grant'->>'environment_sha256'=NEW.environment_sha256) THEN
            RAISE EXCEPTION 'coding attempt freezes exact workspace, tree, environment and profile';
        END IF;
    ELSE
        IF NEW.revision<>OLD.revision+1 OR NEW.updated_at<OLD.updated_at
            OR (to_jsonb(NEW)-ARRAY['revision','state','fenced_at','reason_code','updated_at'])
                IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['revision','state','fenced_at','reason_code','updated_at'])
            OR OLD.state IN ('stopped','result_recorded')
            OR NOT ((OLD.state='ready' AND NEW.state IN ('running','stopped'))
                OR (OLD.state='running' AND NEW.state IN ('stopped','result_recorded'))) THEN
            RAISE EXCEPTION 'coding attempt identity and forward resource phases are immutable';
        END IF;
        IF NEW.state IN ('running','result_recorded') AND NOT app.sa_coding_current(
            NEW.coding_id,NEW.run_id,NEW.assignment_id,NEW.session_id,NEW.execution_epoch) THEN
            RAISE EXCEPTION 'old coding epoch cannot resume or record a current outcome';
        END IF;
        IF NEW.state='result_recorded' AND NOT EXISTS (
            SELECT 1 FROM app.sa_coding_verifications v WHERE v.attempt_id=NEW.attempt_id
            AND v.coding_id=NEW.coding_id AND v.project_id=NEW.project_id) THEN
            RAISE EXCEPTION 'coding result metadata needs a trusted verification fact';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_coding_attempt_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_coding_attempts FOR EACH ROW EXECUTE FUNCTION app.sa_coding_attempt_guard();

CREATE FUNCTION app.sa_coding_operation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'coding operation identity is permanent'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 OR NEW.state<>'queued' OR NOT app.sa_coding_current(
            NEW.coding_id,NEW.run_id,NEW.assignment_id,NEW.session_id,NEW.execution_epoch) THEN
            RAISE EXCEPTION 'new coding operation requires current exact execution authority';
        END IF;
        IF (SELECT count(*) FROM app.sa_coding_operations WHERE attempt_id=NEW.attempt_id)>=256 THEN
            RAISE EXCEPTION 'coding operation journal is bounded per attempt';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM app.sa_coding_attempts a JOIN app.sa_coding_bindings b
                ON b.coding_id=a.coding_id AND b.project_id=a.project_id
            WHERE a.attempt_id=NEW.attempt_id AND a.state='running'
            AND a.environment_sha256=NEW.environment_sha256
            AND b.context_sha256=NEW.context_sha256) THEN
            RAISE EXCEPTION 'coding operation requires its running attempt and frozen context';
        END IF;
        IF NEW.capability IN ('write','execute') AND EXISTS (
            SELECT 1 FROM app.sa_coding_operations WHERE attempt_id=NEW.attempt_id
            AND capability IN ('write','execute') AND state<>'known') THEN
            RAISE EXCEPTION 'pending or unknown mutation must be reconciled before another';
        END IF;
        IF NEW.kind='model' AND NOT EXISTS (
            SELECT 1 FROM app.usage_calls u JOIN app.sa_budget_calls b
                ON b.call_id=u.call_id AND b.ledger_kind='usage'
            WHERE u.call_id=NEW.usage_call_id AND u.run_id=NEW.run_id AND u.status='reserved'
            AND b.project_id=NEW.project_id AND b.component='worker') THEN
            RAISE EXCEPTION 'coding model intent must reference the original shared reservation';
        END IF;
        IF NEW.resource_operation_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM app.sa_scheduler_operations o
            WHERE o.operation_id=NEW.resource_operation_id AND o.assignment_id=NEW.assignment_id
            AND o.project_id=NEW.project_id AND o.run_id=NEW.run_id
            AND o.execution_epoch=NEW.execution_epoch AND o.kind IN ('start','job')
            AND o.request_sha256=NEW.request_sha256) THEN
            RAISE EXCEPTION 'coding command needs its exact original scheduler process intent';
        END IF;
    ELSE
        IF NEW.revision<>OLD.revision+1 OR NEW.updated_at<OLD.updated_at
            OR (to_jsonb(NEW)-ARRAY['revision','state','result_code','result_sha256',
                'observed_at','updated_at']) IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['revision','state','result_code','result_sha256',
                'observed_at','updated_at'])
            OR OLD.state='known'
            OR NOT ((OLD.state='queued' AND NEW.state IN ('running','unknown','known'))
                OR (OLD.state='running' AND NEW.state IN ('unknown','known'))
                OR (OLD.state='unknown' AND NEW.state='known')) THEN
            RAISE EXCEPTION 'coding operation cannot change identity, resend unknown or rewrite truth';
        END IF;
        IF NEW.state='running' AND (NOT app.sa_coding_current(
            NEW.coding_id,NEW.run_id,NEW.assignment_id,NEW.session_id,NEW.execution_epoch)
            OR NOT EXISTS (SELECT 1 FROM app.sa_coding_attempts a
                WHERE a.attempt_id=NEW.attempt_id AND a.state='running')) THEN
            RAISE EXCEPTION 'old coding epoch cannot dispatch another operation';
        END IF;
        -- Late known receipts are facts, not resumed authority or Task success.
        IF NEW.kind='model' AND NEW.state='known' AND NOT EXISTS (
            SELECT 1 FROM app.usage_calls u WHERE u.call_id=NEW.usage_call_id
            AND u.run_id=NEW.run_id AND u.status='known') THEN
            RAISE EXCEPTION 'coding facts never replace original model settlement';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_coding_operation_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_coding_operations FOR EACH ROW EXECUTE FUNCTION app.sa_coding_operation_guard();

CREATE FUNCTION app.sa_coding_payload_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    bound app.sa_coding_bindings%ROWTYPE;
    op app.sa_coding_operations%ROWTYPE;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'erased coding payload keeps minimum permanent metadata'; END IF;
    IF TG_OP='INSERT' THEN
        SELECT * INTO bound FROM app.sa_coding_bindings WHERE coding_id=NEW.coding_id;
        SELECT * INTO op FROM app.sa_coding_operations WHERE operation_id=NEW.operation_id;
        IF NEW.revision<>1 OR NEW.ciphertext IS NULL OR NEW.unavailable_at IS NOT NULL
            OR NEW.context_sha256 IS DISTINCT FROM op.context_sha256
            OR NEW.expires_at>bound.valid_until OR NEW.expires_at<=clock_timestamp()
            OR NOT app.sa_coding_current(op.coding_id,op.run_id,
                op.assignment_id,op.session_id,op.execution_epoch)
            OR (NEW.payload_kind='model_response' AND op.kind<>'model') THEN
            RAISE EXCEPTION 'coding payload requires exact current bounded encrypted context';
        END IF;
    ELSE
        IF OLD.unavailable_at IS NOT NULL OR NEW.revision<>OLD.revision+1
            OR NEW.ciphertext IS NOT NULL OR NEW.unavailable_at IS NULL
            OR NEW.updated_at<OLD.updated_at
            OR (to_jsonb(NEW)-ARRAY['revision','ciphertext','unavailable_at','reason_code','updated_at'])
                IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['revision','ciphertext','unavailable_at','reason_code','updated_at']) THEN
            RAISE EXCEPTION 'coding payload can only be erased once, never extended or restored';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_coding_payload_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_coding_payloads FOR EACH ROW EXECUTE FUNCTION app.sa_coding_payload_guard();

CREATE FUNCTION app.sa_coding_verification_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    op app.sa_coding_operations%ROWTYPE;
    attempt app.sa_coding_attempts%ROWTYPE;
    bound app.sa_coding_bindings%ROWTYPE;
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'coding verification facts are immutable'; END IF;
    SELECT * INTO op FROM app.sa_coding_operations WHERE operation_id=NEW.operation_id;
    SELECT * INTO attempt FROM app.sa_coding_attempts WHERE attempt_id=NEW.attempt_id;
    SELECT * INTO bound FROM app.sa_coding_bindings WHERE coding_id=NEW.coding_id;
    IF op.kind IS DISTINCT FROM 'verification' OR op.state IS DISTINCT FROM 'known'
        OR attempt.state IS DISTINCT FROM 'running'
        OR NEW.operation_sha256 IS DISTINCT FROM op.request_sha256
        OR NEW.tree_sha256 IS DISTINCT FROM op.tree_sha256
        OR NEW.environment_sha256 IS DISTINCT FROM op.environment_sha256
        OR NEW.environment_sha256 IS DISTINCT FROM attempt.environment_sha256
        OR NEW.profile_sha256 IS DISTINCT FROM attempt.profile_sha256
        OR NEW.mandate_sha256 IS DISTINCT FROM bound.mandate_sha256
        OR NEW.acceptance_sha256 IS DISTINCT FROM bound.acceptance_sha256
        OR NEW.receipt_sha256 IS DISTINCT FROM op.result_sha256
        OR NEW.observed_at<op.created_at
        OR NOT app.sa_coding_current(op.coding_id,op.run_id,
            op.assignment_id,op.session_id,op.execution_epoch) THEN
        RAISE EXCEPTION 'coding verification needs current exact trusted journal/assignment provenance';
    END IF;
    IF NEW.result='passed' AND NOT EXISTS (
        SELECT 1 FROM app.sa_coding_operations executed
        JOIN app.sa_scheduler_operations resource
            ON resource.operation_id=executed.resource_operation_id
            AND resource.assignment_id=executed.assignment_id
            AND resource.project_id=executed.project_id
        WHERE executed.operation_id=NEW.execution_operation_id
        AND executed.attempt_id=NEW.attempt_id AND executed.coding_id=NEW.coding_id
        AND executed.project_id=NEW.project_id AND executed.run_id=op.run_id
        AND executed.assignment_id=op.assignment_id AND executed.session_id=op.session_id
        AND executed.execution_epoch=op.execution_epoch
        AND executed.kind='tool' AND executed.capability='execute'
        AND executed.state='known' AND executed.result_code='ok'
        AND executed.tree_sha256=NEW.tree_sha256
        AND executed.environment_sha256=NEW.environment_sha256
        AND executed.observed_at<=NEW.observed_at
        AND resource.run_id=executed.run_id AND resource.session_id=executed.session_id
        AND resource.execution_epoch=executed.execution_epoch
        AND resource.request_sha256=executed.request_sha256
        AND resource.kind IN ('start','job') AND resource.state='known_terminal'
        AND resource.result_code='succeeded' AND resource.observed_at<=NEW.observed_at
    ) THEN
        RAISE EXCEPTION 'passed coding verification requires exact successful actual execution';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_coding_verification_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_coding_verifications FOR EACH ROW EXECUTE FUNCTION app.sa_coding_verification_guard();

CREATE FUNCTION app.sa_coding_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    pair record;
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'coding audit events are immutable'; END IF;
    IF NEW.attempt_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM app.sa_coding_attempts a WHERE a.attempt_id=NEW.attempt_id
        AND a.coding_id=NEW.coding_id AND a.project_id=NEW.project_id) THEN
        RAISE EXCEPTION 'coding event attempt must belong to its exact binding';
    END IF;
    IF NEW.operation_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM app.sa_coding_operations o WHERE o.operation_id=NEW.operation_id
        AND o.coding_id=NEW.coding_id AND o.project_id=NEW.project_id
        AND (NEW.attempt_id IS NULL OR o.attempt_id=NEW.attempt_id)) THEN
        RAISE EXCEPTION 'coding event operation must belong to its exact binding/attempt';
    END IF;
    FOR pair IN SELECT key,value FROM jsonb_each(NEW.details) LOOP
        IF pair.key IN ('state','previous_state') AND (
            jsonb_typeof(pair.value)<>'string' OR pair.value #>> '{}'
                NOT IN ('admitted','revoked','ready','running','stopped',
                    'result_recorded','queued','unknown','known')) THEN
            RAISE EXCEPTION 'coding event state must be a fixed metadata value';
        ELSIF pair.key IN ('epoch','tests_run','tests_failed') AND (
            jsonb_typeof(pair.value)<>'number' OR pair.value::text !~ '^[0-9]{1,18}$') THEN
            RAISE EXCEPTION 'coding event counters must be bounded nonnegative integers';
        ELSIF pair.key IN ('request_sha256','result_sha256','context_sha256','tree_sha256',
            'profile_sha256','receipt_sha256') AND (
            jsonb_typeof(pair.value)<>'string' OR pair.value #>> '{}' !~ '^[a-f0-9]{64}$') THEN
            RAISE EXCEPTION 'coding event references must be exact hashes';
        ELSIF pair.key='verification_result' AND (
            jsonb_typeof(pair.value)<>'string' OR pair.value #>> '{}'
                NOT IN ('passed','failed','incomplete')) THEN
            RAISE EXCEPTION 'coding event verification is a fixed metadata result';
        ELSIF pair.key='payload_kind' AND (
            jsonb_typeof(pair.value)<>'string' OR pair.value #>> '{}'
                NOT IN ('model_response','checkpoint')) THEN
            RAISE EXCEPTION 'coding event payload kind is fixed metadata';
        END IF;
    END LOOP;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_coding_event_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_coding_events FOR EACH ROW EXECUTE FUNCTION app.sa_coding_event_guard();

COMMENT ON TABLE app.sa_coding_bindings IS
    'SA13 original-Mandate bindings only; not a new Task, grant issuer or financial ledger';
COMMENT ON TABLE app.sa_coding_attempts IS
    'SA13 exact original Assignment execution facts; resource results never complete the business Task';
COMMENT ON TABLE app.sa_coding_operations IS
    'SA13 stable IO intent and known/unknown facts; financial truth remains original usage_calls and SA07';
COMMENT ON TABLE app.sa_coding_payloads IS
    'SA13 bounded encrypted payloads; trusted current authority must precede every decryption';
COMMENT ON TABLE app.sa_coding_verifications IS
    'SA13 trusted exact-tree verification metadata; bare worker/model assertions are not proof';
COMMENT ON TABLE app.sa_coding_events IS
    'SA13 immutable typed metadata only; no Source/model/log/credential plaintext';
