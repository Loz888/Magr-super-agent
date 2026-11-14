-- SA14 REVIEW DRAFT. 0047 has not been allocated or installed.
-- Apply only after a fresh Harness ownership confirmation. Do not edit old SQL.
-- Original Engine/held PG connection remains the only writer. No runtime starts.
-- These tables contain bounded references, restrictions and observed facts only.

CREATE FUNCTION app.sa_async_paths_valid(paths jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(paths) IS DISTINCT FROM 'array' THEN false ELSE
        jsonb_array_length(paths)<=64 AND NOT EXISTS (
            SELECT 1 FROM jsonb_array_elements(paths) p(value)
            WHERE jsonb_typeof(value)<>'string' OR length(value#>>'{}') NOT BETWEEN 1 AND 256
                OR value#>>'{}' ~ '(^/|/$|//|[[:cntrl:]])'
                OR translate(value#>>'{}',':*?[]%','')<>value#>>'{}'
                OR strpos(value#>>'{}',chr(92))>0
                OR string_to_array(value#>>'{}','/') && ARRAY['.','..']
        ) AND (SELECT count(*)=count(DISTINCT value) FROM jsonb_array_elements(paths))
        AND paths=(SELECT coalesce(jsonb_agg(value ORDER BY value COLLATE "C"),'[]')
            FROM jsonb_array_elements_text(paths) p(value))
    END;
$$;
CREATE FUNCTION app.sa_async_restriction_valid(value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(value) IS DISTINCT FROM 'object'
        OR NOT value ?& ARRAY['deny_write_paths','deny_commands']
        OR value-ARRAY['deny_write_paths','deny_commands']<>'{}'::jsonb
        OR jsonb_typeof(value->'deny_commands') IS DISTINCT FROM 'array'
    THEN false ELSE app.sa_async_paths_valid(value->'deny_write_paths')
        AND jsonb_array_length(value->'deny_commands')<=32
        AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(value->'deny_commands') c(item)
            WHERE jsonb_typeof(item)<>'string' OR item#>>'{}' !~ '^[A-Za-z0-9][A-Za-z0-9_+.-]{0,63}$')
        AND (SELECT count(*)=count(DISTINCT item)
            FROM jsonb_array_elements(value->'deny_commands') c(item))
        AND value->'deny_commands'=(SELECT coalesce(jsonb_agg(item ORDER BY item COLLATE "C"),'[]')
            FROM jsonb_array_elements_text(value->'deny_commands') c(item)) END;
$$;

CREATE TABLE app.sa_async_bindings (
    async_id uuid PRIMARY KEY,
    coding_id uuid NOT NULL UNIQUE,
    project_id text COLLATE "C" NOT NULL,
    run_id uuid NOT NULL UNIQUE,
    coding_revision bigint NOT NULL CHECK (coding_revision>0),
    assignment_id uuid NOT NULL,
    session_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch>0),
    mandate_sha256 char(64) NOT NULL CHECK (mandate_sha256 ~ '^[a-f0-9]{64}$'),
    profile_sha256 char(64) NOT NULL CHECK (profile_sha256 ~ '^[a-f0-9]{64}$'),
    initial_context_sha256 char(64) NOT NULL CHECK (initial_context_sha256 ~ '^[a-f0-9]{64}$'),
    scope_sha256 char(64) NOT NULL CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    request_id uuid NOT NULL,
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    review_sha256 char(64) NOT NULL CHECK (review_sha256 ~ '^[a-f0-9]{64}$'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    input_revision bigint NOT NULL DEFAULT 0 CHECK (input_revision>=0),
    received_sequence bigint NOT NULL DEFAULT 0 CHECK (received_sequence>=0),
    applied_sequence bigint NOT NULL DEFAULT 0 CHECK (applied_sequence BETWEEN 0 AND received_sequence),
    restriction jsonb NOT NULL DEFAULT '{"deny_write_paths":[],"deny_commands":[]}'
        CHECK (app.sa_async_restriction_valid(restriction)),
    current_checkpoint_id uuid,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','fenced','closed')),
    reason_code text CHECK (reason_code ~ '^[A-Z0-9_]{1,100}$'),
    fenced_at timestamptz,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(async_id,project_id),
    UNIQUE(async_id,coding_id,project_id,run_id,assignment_id,session_id,execution_epoch),
    UNIQUE(project_id,request_id),
    FOREIGN KEY(coding_id,project_id,run_id)
        REFERENCES app.sa_coding_bindings(coding_id,project_id,run_id),
    FOREIGN KEY(assignment_id,project_id,session_id,run_id,execution_epoch)
        REFERENCES app.sa_scheduler_assignments
            (assignment_id,project_id,session_id,run_id,execution_epoch),
    CHECK (expires_at>created_at AND updated_at>=created_at),
    CHECK ((state='active' AND fenced_at IS NULL AND reason_code IS NULL)
        OR (state IN ('fenced','closed') AND fenced_at>=created_at AND reason_code IS NOT NULL))
);

CREATE TABLE app.sa_async_checkpoints (
    checkpoint_id uuid PRIMARY KEY,
    async_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    scope_sha256 char(64) NOT NULL CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    request_id uuid NOT NULL,
    stage text NOT NULL CHECK (stage IN ('before_model','after_model','before_tool',
        'before_command','command_poll','before_result')),
    input_revision bigint NOT NULL CHECK (input_revision>=0),
    through_sequence bigint NOT NULL CHECK (through_sequence>=0),
    restriction jsonb NOT NULL CHECK (app.sa_async_restriction_valid(restriction)),
    context_sha256 char(64) NOT NULL CHECK (context_sha256 ~ '^[a-f0-9]{64}$'),
    tree_sha256 char(64) NOT NULL CHECK (tree_sha256 ~ '^[a-f0-9]{64}$'),
    environment_sha256 char(64) NOT NULL CHECK (environment_sha256 ~ '^[a-f0-9]{64}$'),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(checkpoint_id,async_id,project_id),
    UNIQUE(async_id,request_id),
    FOREIGN KEY(async_id,project_id) REFERENCES app.sa_async_bindings(async_id,project_id),
    CHECK (expires_at>created_at AND expires_at<=created_at+interval '30 minutes')
);
ALTER TABLE app.sa_async_bindings ADD CONSTRAINT sa_async_published_checkpoint
    FOREIGN KEY(current_checkpoint_id,async_id,project_id)
    REFERENCES app.sa_async_checkpoints(checkpoint_id,async_id,project_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE app.sa_async_inputs (
    input_id uuid PRIMARY KEY,
    async_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    scope_sha256 char(64) NOT NULL CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    sequence bigint NOT NULL CHECK (sequence>0),
    input_revision bigint NOT NULL CHECK (input_revision>=0),
    request_id uuid NOT NULL,
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    kind text NOT NULL CHECK (kind IN ('context','restriction','invalidation','job_progress')),
    origin text NOT NULL CHECK (origin IN ('owner','source','system','job')),
    state text NOT NULL DEFAULT 'received' CHECK (state IN ('received','applied','blocked')),
    source_input_id uuid REFERENCES app.source_ingress_inputs(input_id),
    source_instance_id uuid REFERENCES app.source_instances(source_instance_id),
    version_fingerprint char(64) CHECK (version_fingerprint ~ '^[a-f0-9]{64}$'),
    source_state_sha256 char(64) CHECK (source_state_sha256 ~ '^[a-f0-9]{64}$'),
    identity_id uuid REFERENCES app.source_identities(identity_id),
    identity_revision bigint CHECK (identity_revision>0),
    policy_id uuid REFERENCES app.source_policies(policy_id),
    policy_revision bigint CHECK (policy_revision>0),
    role_binding_id uuid REFERENCES app.source_role_bindings(binding_id),
    role_revision bigint CHECK (role_revision>0),
    owner_id uuid REFERENCES app.web_accounts(user_id),
    resource_operation_id uuid REFERENCES app.sa_scheduler_operations(operation_id),
    restriction jsonb CHECK (restriction IS NULL OR app.sa_async_restriction_valid(restriction)),
    applied_checkpoint_id uuid,
    reason_code text CHECK (reason_code ~ '^[A-Z0-9_]{1,100}$'),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(async_id,sequence),
    UNIQUE(async_id,request_id),
    UNIQUE(async_id,request_sha256),
    UNIQUE(async_id,source_input_id),
    FOREIGN KEY(async_id,project_id) REFERENCES app.sa_async_bindings(async_id,project_id),
    FOREIGN KEY(applied_checkpoint_id,async_id,project_id)
        REFERENCES app.sa_async_checkpoints(checkpoint_id,async_id,project_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (expires_at>received_at AND expires_at<=received_at+interval '1 hour'),
    CHECK (updated_at>=received_at),
    CHECK ((state='received' AND applied_checkpoint_id IS NULL AND reason_code IS NULL)
        OR (state='applied' AND applied_checkpoint_id IS NOT NULL AND reason_code IS NULL)
        OR (state='blocked' AND applied_checkpoint_id IS NULL AND reason_code IS NOT NULL)),
    CHECK ((kind='restriction')=(restriction IS NOT NULL)),
    CHECK ((origin='source' AND source_input_id IS NOT NULL AND source_instance_id IS NOT NULL
            AND version_fingerprint IS NOT NULL AND source_state_sha256 IS NOT NULL
            AND identity_id IS NOT NULL AND identity_revision IS NOT NULL
            AND policy_id IS NOT NULL AND policy_revision IS NOT NULL AND owner_id IS NULL)
        OR (origin<>'source' AND source_input_id IS NULL AND source_instance_id IS NULL
            AND version_fingerprint IS NULL AND source_state_sha256 IS NULL
            AND identity_id IS NULL AND identity_revision IS NULL
            AND policy_id IS NULL AND policy_revision IS NULL AND role_binding_id IS NULL
            AND role_revision IS NULL)),
    CHECK ((origin='owner' AND owner_id IS NOT NULL AND kind='restriction')
        OR (origin='source' AND kind IN ('context','restriction') AND owner_id IS NULL)
        OR (origin='system' AND kind='invalidation' AND owner_id IS NULL)
        OR (origin='job' AND kind='job_progress' AND resource_operation_id IS NOT NULL
            AND owner_id IS NULL)),
    CHECK (origin='job' OR resource_operation_id IS NULL),
    CHECK (num_nonnulls(role_binding_id,role_revision) IN (0,2)),
    CHECK (origin<>'source' OR kind<>'restriction' OR role_binding_id IS NOT NULL)
);
CREATE INDEX sa_async_unapplied_inputs ON app.sa_async_inputs(async_id,sequence)
    WHERE state='received';
CREATE INDEX sa_async_source_dependencies ON app.sa_async_inputs(source_input_id,async_id)
    WHERE origin='source';

CREATE TABLE app.sa_async_operation_views (
    operation_id uuid PRIMARY KEY REFERENCES app.sa_coding_operations(operation_id),
    async_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    checkpoint_id uuid NOT NULL,
    input_revision bigint NOT NULL CHECK (input_revision>=0),
    scope_sha256 char(64) NOT NULL CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    context_sha256 char(64) NOT NULL CHECK (context_sha256 ~ '^[a-f0-9]{64}$'),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(operation_id,async_id,project_id),
    FOREIGN KEY(checkpoint_id,async_id,project_id)
        REFERENCES app.sa_async_checkpoints(checkpoint_id,async_id,project_id),
    CHECK (expires_at>created_at)
);

CREATE TABLE app.sa_async_jobs (
    job_id uuid PRIMARY KEY,
    async_id uuid NOT NULL,
    coding_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    run_id uuid NOT NULL,
    assignment_id uuid NOT NULL,
    session_id uuid NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch>0),
    checkpoint_id uuid NOT NULL,
    coding_operation_id uuid NOT NULL UNIQUE REFERENCES app.sa_coding_operations(operation_id),
    resource_operation_id uuid NOT NULL UNIQUE REFERENCES app.sa_scheduler_operations(operation_id),
    scope_sha256 char(64) NOT NULL CHECK (scope_sha256 ~ '^[a-f0-9]{64}$'),
    input_revision bigint NOT NULL CHECK (input_revision>=0),
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    tree_sha256 char(64) NOT NULL CHECK (tree_sha256 ~ '^[a-f0-9]{64}$'),
    environment_sha256 char(64) NOT NULL CHECK (environment_sha256 ~ '^[a-f0-9]{64}$'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    state text NOT NULL DEFAULT 'prepared'
        CHECK (state IN ('prepared','running','stop_requested','unknown','known')),
    result_code text CHECK (result_code IN ('succeeded','failed','cancelled','absent')),
    receipt_sha256 char(64) CHECK (receipt_sha256 ~ '^[a-f0-9]{64}$'),
    observed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(async_id,coding_id,project_id,run_id,assignment_id,session_id,execution_epoch)
        REFERENCES app.sa_async_bindings
            (async_id,coding_id,project_id,run_id,assignment_id,session_id,execution_epoch),
    FOREIGN KEY(checkpoint_id,async_id,project_id)
        REFERENCES app.sa_async_checkpoints(checkpoint_id,async_id,project_id),
    CHECK (updated_at>=created_at),
    CHECK ((state='known' AND result_code IS NOT NULL AND receipt_sha256 IS NOT NULL
            AND observed_at>=created_at)
        OR (state<>'known' AND result_code IS NULL AND receipt_sha256 IS NULL AND observed_at IS NULL))
);

CREATE TABLE app.sa_async_events (
    event_id bigserial PRIMARY KEY,
    async_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    kind text NOT NULL CHECK (kind IN ('admitted','received','applied','blocked','fenced',
        'job_prepared','job_running','job_stop_requested','job_unknown','job_known','recovered')),
    input_id uuid REFERENCES app.sa_async_inputs(input_id),
    job_id uuid REFERENCES app.sa_async_jobs(job_id),
    checkpoint_id uuid,
    input_revision bigint NOT NULL CHECK (input_revision>=0),
    reason_code text CHECK (reason_code ~ '^[A-Z0-9_]{1,100}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(async_id,project_id) REFERENCES app.sa_async_bindings(async_id,project_id),
    FOREIGN KEY(checkpoint_id,async_id,project_id)
        REFERENCES app.sa_async_checkpoints(checkpoint_id,async_id,project_id)
);
CREATE INDEX sa_async_event_cursor ON app.sa_async_events(async_id,event_id);

CREATE FUNCTION app.sa_async_current(identifier uuid) RETURNS boolean LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS (SELECT 1 FROM app.sa_async_bindings b
        WHERE b.async_id=identifier AND b.state='active' AND b.expires_at>clock_timestamp()
        AND app.sa_coding_current(b.coding_id,b.run_id,b.assignment_id,b.session_id,b.execution_epoch));
$$;

CREATE FUNCTION app.sa_async_binding_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'async scope identity is permanent'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 OR NEW.input_revision<>0 OR NEW.received_sequence<>0
            OR NEW.applied_sequence<>0 OR NEW.current_checkpoint_id IS NOT NULL
            OR NEW.state<>'active' OR NEW.restriction<>'{"deny_write_paths":[],"deny_commands":[]}'::jsonb
            OR NOT app.sa_coding_current(NEW.coding_id,NEW.run_id,
                NEW.assignment_id,NEW.session_id,NEW.execution_epoch)
            OR NOT EXISTS (SELECT 1 FROM app.sa_coding_bindings c
                JOIN app.sa_scheduler_assignments a ON a.assignment_id=NEW.assignment_id
                WHERE c.coding_id=NEW.coding_id AND c.project_id=NEW.project_id
                AND c.revision=NEW.coding_revision AND c.mandate_sha256=NEW.mandate_sha256
                AND c.profile_sha256=NEW.profile_sha256 AND c.context_sha256=NEW.initial_context_sha256
                AND c.execution_mode='fixture' AND NEW.expires_at<=c.valid_until
                AND a.workspace_id=NEW.workspace_id) THEN
            RAISE EXCEPTION 'async admission requires the exact active original controlled scope';
        END IF;
    ELSE
        IF NEW.revision<>OLD.revision+1 OR OLD.state='closed'
            OR NEW.updated_at<OLD.updated_at OR NEW.received_sequence<OLD.received_sequence
            OR NEW.applied_sequence<OLD.applied_sequence OR NEW.input_revision<OLD.input_revision
            OR (OLD.state<>'active' AND NEW.state='active')
            OR NOT ((NEW.restriction->'deny_write_paths') @> (OLD.restriction->'deny_write_paths'))
            OR NOT ((NEW.restriction->'deny_commands') @> (OLD.restriction->'deny_commands'))
            OR (to_jsonb(NEW)-ARRAY['revision','input_revision','received_sequence','applied_sequence',
                'restriction','current_checkpoint_id','state','reason_code','fenced_at','updated_at'])
                IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['revision','input_revision','received_sequence','applied_sequence',
                'restriction','current_checkpoint_id','state','reason_code','fenced_at','updated_at']) THEN
            RAISE EXCEPTION 'async binding cannot expand authority, rewind inputs or revive a fence';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_async_binding_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_async_bindings FOR EACH ROW EXECUTE FUNCTION app.sa_async_binding_guard();

CREATE FUNCTION app.sa_async_checkpoint_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE b app.sa_async_bindings%ROWTYPE;
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'safe-point snapshots are immutable'; END IF;
    SELECT * INTO b FROM app.sa_async_bindings WHERE async_id=NEW.async_id FOR UPDATE;
    IF NOT FOUND OR NOT app.sa_async_current(NEW.async_id) OR NEW.scope_sha256<>b.scope_sha256
        OR NEW.project_id<>b.project_id OR NEW.input_revision<>b.input_revision
        OR NEW.through_sequence<>b.received_sequence OR NEW.expires_at>b.expires_at
        OR NOT ((NEW.restriction->'deny_write_paths') @> (b.restriction->'deny_write_paths'))
        OR NOT ((NEW.restriction->'deny_commands') @> (b.restriction->'deny_commands'))
        OR (SELECT count(*) FROM app.sa_async_checkpoints WHERE async_id=NEW.async_id)>=512 THEN
        RAISE EXCEPTION 'safe point requires bounded current exact original authority';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_async_checkpoint_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_async_checkpoints FOR EACH ROW EXECUTE FUNCTION app.sa_async_checkpoint_guard();

CREATE FUNCTION app.sa_async_input_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE b app.sa_async_bindings%ROWTYPE;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'async input disposition is permanent'; END IF;
    SELECT * INTO b FROM app.sa_async_bindings WHERE async_id=NEW.async_id FOR UPDATE;
    IF NOT FOUND OR b.project_id<>NEW.project_id OR b.scope_sha256<>NEW.scope_sha256 THEN
        RAISE EXCEPTION 'async input cannot change target';
    END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.sequence<>b.received_sequence OR NEW.input_revision<>b.input_revision
            OR NEW.state='applied' OR NEW.sequence<=0
            OR (NEW.origin<>'system' AND (SELECT count(*) FROM app.sa_async_inputs
                WHERE async_id=NEW.async_id)>=256)
            OR (NEW.state='received' AND NEW.origin<>'system' AND NOT app.sa_async_current(NEW.async_id)) THEN
            RAISE EXCEPTION 'async input requires its allocated sequence and bounded current scope';
        END IF;
        IF NEW.origin='source' AND NOT EXISTS (
            SELECT 1 FROM app.source_ingress_inputs i
            WHERE i.input_id=NEW.source_input_id AND i.source_instance_id=NEW.source_instance_id
            AND i.project_id=NEW.project_id AND i.version_fingerprint=NEW.version_fingerprint
            AND i.identity_id=NEW.identity_id AND i.identity_revision=NEW.identity_revision
            AND i.policy_id=NEW.policy_id AND i.policy_revision=NEW.policy_revision) THEN
            RAISE EXCEPTION 'async source reference must match its immutable canonical fact';
        END IF;
        IF NEW.origin='source' AND NEW.kind='restriction' AND NEW.state<>'blocked' AND NOT EXISTS (
            SELECT 1 FROM app.source_role_bindings role
            JOIN app.source_task_controllers controller ON controller.binding_id=role.binding_id
            JOIN app.source_identities identity ON identity.identity_id=role.identity_id
            JOIN app.sa_coding_bindings c ON c.coding_id=b.coding_id
            JOIN app.source_ingress_inputs i ON i.input_id=NEW.source_input_id
            WHERE role.binding_id=NEW.role_binding_id AND role.identity_id=NEW.identity_id
            AND role.project_id=NEW.project_id AND role.revision=NEW.role_revision
            AND role.active AND role.roles @> '["task_controller"]'::jsonb
            AND controller.active AND controller.ticket_id=c.ticket_id AND controller.thread_id=c.thread_id
            AND identity.revoked_at IS NULL AND identity.actor_kind='human' AND NOT i.context_only) THEN
            RAISE EXCEPTION 'Source restriction requires current human exact Ticket control';
        END IF;
    ELSE
        IF OLD.state<>'received' OR NEW.state NOT IN ('applied','blocked')
            OR NEW.updated_at<OLD.updated_at
            OR (to_jsonb(NEW)-ARRAY['state','applied_checkpoint_id','reason_code','updated_at'])
                IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['state','applied_checkpoint_id','reason_code','updated_at']) THEN
            RAISE EXCEPTION 'async input is consumed once and never rewritten';
        END IF;
        IF NEW.state='applied' AND (NOT app.sa_async_current(NEW.async_id) OR NOT EXISTS (
            SELECT 1 FROM app.sa_async_checkpoints c WHERE c.checkpoint_id=NEW.applied_checkpoint_id
            AND c.async_id=NEW.async_id AND c.project_id=NEW.project_id
            AND c.through_sequence>=NEW.sequence AND c.input_revision=b.input_revision
            AND c.expires_at<=NEW.expires_at)) THEN
            RAISE EXCEPTION 'applied requires a committed current bounded checkpoint';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_async_input_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_async_inputs FOR EACH ROW EXECUTE FUNCTION app.sa_async_input_guard();

CREATE FUNCTION app.sa_async_view_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'operation view identity is permanent'; END IF;
    IF NOT EXISTS (SELECT 1 FROM app.sa_async_bindings b
        JOIN app.sa_async_checkpoints c ON c.checkpoint_id=b.current_checkpoint_id
        JOIN app.sa_coding_operations o ON o.operation_id=NEW.operation_id AND o.coding_id=b.coding_id
        WHERE b.async_id=NEW.async_id AND NEW.project_id=b.project_id
        AND c.checkpoint_id=NEW.checkpoint_id AND c.input_revision=b.input_revision
        AND NEW.input_revision=c.input_revision AND NEW.scope_sha256=b.scope_sha256
        AND NEW.context_sha256=c.context_sha256 AND NEW.expires_at=c.expires_at
        AND o.assignment_id=b.assignment_id AND o.session_id=b.session_id
        AND o.execution_epoch=b.execution_epoch AND o.tree_sha256=c.tree_sha256
        AND o.environment_sha256=c.environment_sha256 AND c.expires_at>clock_timestamp()
        AND app.sa_async_current(b.async_id)) THEN
        RAISE EXCEPTION 'original operation must freeze its current async input dependencies';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_async_view_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_async_operation_views FOR EACH ROW EXECUTE FUNCTION app.sa_async_view_guard();

CREATE FUNCTION app.sa_async_link_original_operation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE b app.sa_async_bindings%ROWTYPE; c app.sa_async_checkpoints%ROWTYPE;
BEGIN
    SELECT * INTO b FROM app.sa_async_bindings WHERE coding_id=NEW.coding_id;
    IF NOT FOUND THEN RETURN NEW; END IF;
    IF TG_OP='INSERT' THEN
        SELECT * INTO c FROM app.sa_async_checkpoints WHERE checkpoint_id=b.current_checkpoint_id;
        IF NOT FOUND THEN RAISE EXCEPTION 'async-managed operations require a published safe point'; END IF;
        INSERT INTO app.sa_async_operation_views(operation_id,async_id,project_id,checkpoint_id,
            input_revision,scope_sha256,context_sha256,expires_at)
        VALUES(NEW.operation_id,b.async_id,b.project_id,c.checkpoint_id,c.input_revision,
            b.scope_sha256,c.context_sha256,c.expires_at);
    ELSIF NEW.state='running' AND OLD.state IS DISTINCT FROM NEW.state THEN
        IF NOT EXISTS (SELECT 1 FROM app.sa_async_operation_views v
            WHERE v.operation_id=NEW.operation_id AND v.async_id=b.async_id
            AND v.input_revision=b.input_revision AND v.expires_at>clock_timestamp()
            AND app.sa_async_current(b.async_id)) THEN
            RAISE EXCEPTION 'new input invalidates dispatch of the entire old model view';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_async_link_original_operation AFTER INSERT OR UPDATE
    ON app.sa_coding_operations FOR EACH ROW EXECUTE FUNCTION app.sa_async_link_original_operation();

CREATE FUNCTION app.sa_async_payload_bound() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v app.sa_async_operation_views%ROWTYPE;
BEGIN
    IF NEW.unavailable_at IS NOT NULL THEN RETURN NEW; END IF;
    SELECT * INTO v FROM app.sa_async_operation_views WHERE operation_id=NEW.operation_id;
    IF FOUND AND (NEW.expires_at>v.expires_at OR NOT app.sa_async_current(v.async_id)
        OR NOT EXISTS (SELECT 1 FROM app.sa_async_bindings b WHERE b.async_id=v.async_id
            AND b.input_revision=v.input_revision)) THEN
        RAISE EXCEPTION 'encrypted Coding payload may not outlive its async Source context';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_async_payload_bound BEFORE INSERT OR UPDATE
    ON app.sa_coding_payloads FOR EACH ROW EXECUTE FUNCTION app.sa_async_payload_bound();

CREATE FUNCTION app.sa_async_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'job identity and unknown outcome are permanent'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 OR NEW.state<>'prepared' OR NOT app.sa_async_current(NEW.async_id)
            OR NOT EXISTS (SELECT 1 FROM app.sa_async_operation_views v
                JOIN app.sa_coding_operations c ON c.operation_id=v.operation_id
                JOIN app.sa_scheduler_operations r ON r.operation_id=c.resource_operation_id
                WHERE v.operation_id=NEW.coding_operation_id AND v.async_id=NEW.async_id
                AND v.checkpoint_id=NEW.checkpoint_id AND v.project_id=NEW.project_id
                AND v.scope_sha256=NEW.scope_sha256 AND v.input_revision=NEW.input_revision
                AND c.coding_id=NEW.coding_id AND c.assignment_id=NEW.assignment_id
                AND c.session_id=NEW.session_id AND c.execution_epoch=NEW.execution_epoch
                AND c.run_id=NEW.run_id AND c.capability='execute' AND r.kind='job'
                AND c.resource_operation_id=NEW.resource_operation_id
                AND c.request_sha256=NEW.request_sha256 AND r.request_sha256=NEW.request_sha256
                AND c.tree_sha256=NEW.tree_sha256 AND c.environment_sha256=NEW.environment_sha256)
            OR (SELECT count(*) FROM app.sa_async_jobs WHERE async_id=NEW.async_id)>=128 THEN
            RAISE EXCEPTION 'job must wrap one exact original Coding and SA12 resource intent';
        END IF;
    ELSE
        IF NEW.revision<>OLD.revision+1 OR NEW.updated_at<OLD.updated_at OR OLD.state='known'
            OR (to_jsonb(NEW)-ARRAY['revision','state','result_code','receipt_sha256','observed_at','updated_at'])
                IS DISTINCT FROM
               (to_jsonb(OLD)-ARRAY['revision','state','result_code','receipt_sha256','observed_at','updated_at'])
            OR NOT ((OLD.state='prepared' AND NEW.state IN ('running','stop_requested','unknown','known'))
                OR (OLD.state='running' AND NEW.state IN ('stop_requested','unknown','known'))
                OR (OLD.state='stop_requested' AND NEW.state IN ('unknown','known'))
                OR (OLD.state='unknown' AND NEW.state='known')) THEN
            RAISE EXCEPTION 'job cannot be replayed, retargeted or rewrite observed truth';
        END IF;
        IF NEW.state='running' AND (NOT app.sa_async_current(NEW.async_id)
            OR NOT EXISTS (SELECT 1 FROM app.sa_async_bindings b WHERE b.async_id=NEW.async_id
                AND b.input_revision=NEW.input_revision)) THEN
            RAISE EXCEPTION 'stale job cannot start';
        END IF;
        IF NEW.state='known' AND NOT EXISTS (SELECT 1 FROM app.sa_scheduler_operations r
            WHERE r.operation_id=NEW.resource_operation_id AND r.assignment_id=NEW.assignment_id
            AND r.session_id=NEW.session_id AND r.execution_epoch=NEW.execution_epoch
            AND r.request_sha256=NEW.request_sha256 AND r.state='known_terminal'
            AND r.result_code=NEW.result_code AND r.result_sha256=NEW.receipt_sha256
            AND r.observed_at=NEW.observed_at) THEN
            RAISE EXCEPTION 'known job requires the original exact trusted resource receipt';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_async_job_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_async_jobs FOR EACH ROW EXECUTE FUNCTION app.sa_async_job_guard();

CREATE FUNCTION app.sa_async_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'async metadata events are immutable'; END IF;
    IF NOT EXISTS (SELECT 1 FROM app.sa_async_bindings b WHERE b.async_id=NEW.async_id
        AND b.project_id=NEW.project_id AND b.input_revision>=NEW.input_revision)
        OR (NEW.input_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM app.sa_async_inputs i
            WHERE i.input_id=NEW.input_id AND i.async_id=NEW.async_id))
        OR (NEW.job_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM app.sa_async_jobs j
            WHERE j.job_id=NEW.job_id AND j.async_id=NEW.async_id)) THEN
        RAISE EXCEPTION 'async metadata event cannot borrow another target identity';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_async_event_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_async_events FOR EACH ROW EXECUTE FUNCTION app.sa_async_event_guard();

-- A committed loss of authority remains fenced even if access is later restored.
-- Security invalidation bypasses ordinary intake quotas and retains no body.
CREATE FUNCTION app.sa_async_invalidate(identifier uuid, reason text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE b app.sa_async_bindings%ROWTYPE; identifier_input uuid; at_time timestamptz;
BEGIN
    SELECT * INTO b FROM app.sa_async_bindings WHERE async_id=identifier FOR UPDATE;
    IF NOT FOUND OR b.state<>'active' THEN RETURN; END IF;
    at_time:=clock_timestamp(); identifier_input:=gen_random_uuid();
    UPDATE app.sa_async_bindings SET state='fenced',fenced_at=at_time,reason_code=reason,
        revision=revision+1,input_revision=input_revision+1,received_sequence=received_sequence+1,
        updated_at=at_time WHERE async_id=identifier RETURNING * INTO b;
    INSERT INTO app.sa_async_inputs(input_id,async_id,project_id,scope_sha256,sequence,input_revision,
        request_id,request_sha256,kind,origin,received_at,expires_at,updated_at)
    VALUES(identifier_input,b.async_id,b.project_id,b.scope_sha256,b.received_sequence,b.input_revision,
        identifier_input,encode(sha256(convert_to(identifier::text||':'||reason,'UTF8')),'hex'),
        'invalidation','system',at_time,at_time+interval '1 hour',at_time);
    INSERT INTO app.sa_async_events(async_id,project_id,kind,input_id,input_revision,reason_code)
        VALUES(b.async_id,b.project_id,'fenced',identifier_input,b.input_revision,reason);
    UPDATE app.sa_async_jobs SET state='stop_requested',revision=revision+1,updated_at=at_time
        WHERE async_id=identifier AND state IN ('prepared','running');
    UPDATE app.sa_coding_payloads p SET ciphertext=NULL,unavailable_at=at_time,
        reason_code=reason,revision=p.revision+1,updated_at=at_time
        FROM app.sa_async_operation_views v WHERE v.operation_id=p.operation_id
        AND v.async_id=identifier AND p.unavailable_at IS NULL;
END $$;

CREATE FUNCTION app.sa_async_fence_source_input(identifier uuid, reason text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE target uuid;
BEGIN
    FOR target IN SELECT DISTINCT b.async_id FROM app.sa_async_bindings b
        JOIN app.sa_coding_bindings c ON c.coding_id=b.coding_id
        WHERE b.state='active' AND (c.input_id=identifier OR EXISTS (
            SELECT 1 FROM app.sa_async_inputs i
            WHERE i.async_id=b.async_id AND i.source_input_id=identifier))
    LOOP PERFORM app.sa_async_invalidate(target,reason); END LOOP;
END $$;

CREATE FUNCTION app.sa_async_ingress_input_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' OR NEW.unavailable_at IS DISTINCT FROM OLD.unavailable_at
        OR NEW.metadata IS DISTINCT FROM OLD.metadata OR NEW.sender_ref IS DISTINCT FROM OLD.sender_ref THEN
        PERFORM app.sa_async_fence_source_input(OLD.input_id,'SOURCE_ASYNC_SOURCE_INVALIDATED');
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER sa_async_ingress_input_changed AFTER UPDATE OR DELETE
    ON app.source_ingress_inputs FOR EACH ROW EXECUTE FUNCTION app.sa_async_ingress_input_changed();

CREATE FUNCTION app.sa_async_ingress_body_removed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM app.sa_async_fence_source_input(OLD.input_id,'SOURCE_ASYNC_BODY_UNAVAILABLE');
    RETURN NULL;
END $$;
CREATE TRIGGER sa_async_ingress_body_removed AFTER DELETE
    ON app.source_ingress_bodies FOR EACH ROW EXECUTE FUNCTION app.sa_async_ingress_body_removed();

CREATE FUNCTION app.sa_async_role_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid;
BEGIN
    FOR target IN SELECT DISTINCT b.async_id FROM app.sa_async_bindings b
        JOIN app.sa_coding_bindings c ON c.coding_id=b.coding_id
        WHERE b.state='active' AND (c.identity_id=OLD.identity_id OR EXISTS (
            SELECT 1 FROM app.sa_async_inputs i WHERE i.async_id=b.async_id
            AND (i.identity_id=OLD.identity_id OR i.role_binding_id=OLD.binding_id)))
    LOOP PERFORM app.sa_async_invalidate(target,'SOURCE_ASYNC_ROLE_CHANGED'); END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER sa_async_role_changed AFTER UPDATE OR DELETE
    ON app.source_role_bindings FOR EACH ROW EXECUTE FUNCTION app.sa_async_role_changed();

CREATE FUNCTION app.sa_async_controller_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid;
BEGIN
    FOR target IN SELECT DISTINCT b.async_id FROM app.sa_async_bindings b
        JOIN app.sa_coding_bindings c ON c.coding_id=b.coding_id
        WHERE b.state='active' AND c.ticket_id=OLD.ticket_id AND (EXISTS (
            SELECT 1 FROM app.source_role_bindings role WHERE role.binding_id=OLD.binding_id
            AND role.identity_id=c.identity_id) OR EXISTS (
            SELECT 1 FROM app.sa_async_inputs i WHERE i.async_id=b.async_id
            AND i.role_binding_id=OLD.binding_id))
    LOOP PERFORM app.sa_async_invalidate(target,'SOURCE_ASYNC_CONTROLLER_CHANGED'); END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER sa_async_controller_changed AFTER UPDATE OR DELETE
    ON app.source_task_controllers FOR EACH ROW EXECUTE FUNCTION app.sa_async_controller_changed();

CREATE FUNCTION app.sa_async_identity_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid;
BEGIN
    FOR target IN SELECT DISTINCT b.async_id FROM app.sa_async_bindings b
        JOIN app.sa_coding_bindings c ON c.coding_id=b.coding_id
        WHERE b.state='active' AND (c.identity_id=OLD.identity_id OR EXISTS (
            SELECT 1 FROM app.sa_async_inputs i WHERE i.async_id=b.async_id
            AND i.identity_id=OLD.identity_id))
    LOOP PERFORM app.sa_async_invalidate(target,'SOURCE_ASYNC_IDENTITY_CHANGED'); END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER sa_async_identity_changed AFTER UPDATE
    ON app.source_identities FOR EACH ROW EXECUTE FUNCTION app.sa_async_identity_changed();

CREATE FUNCTION app.sa_async_policy_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid;
BEGIN
    FOR target IN SELECT DISTINCT b.async_id FROM app.sa_async_bindings b
        JOIN app.sa_coding_bindings c ON c.coding_id=b.coding_id
        WHERE b.state='active' AND (c.policy_id=OLD.policy_id OR EXISTS (
            SELECT 1 FROM app.sa_async_inputs i WHERE i.async_id=b.async_id
            AND i.policy_id=OLD.policy_id))
    LOOP PERFORM app.sa_async_invalidate(target,'SOURCE_ASYNC_POLICY_CHANGED'); END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER sa_async_policy_changed AFTER UPDATE
    ON app.source_policies FOR EACH ROW EXECUTE FUNCTION app.sa_async_policy_changed();

CREATE FUNCTION app.sa_async_ingress_control_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid;
BEGIN
    FOR target IN SELECT DISTINCT b.async_id FROM app.sa_async_bindings b
        JOIN app.sa_coding_bindings c ON c.coding_id=b.coding_id
        WHERE b.state='active' AND (c.source_instance_id=OLD.source_instance_id OR EXISTS (
            SELECT 1 FROM app.sa_async_inputs i WHERE i.async_id=b.async_id
            AND i.source_instance_id=OLD.source_instance_id))
    LOOP PERFORM app.sa_async_invalidate(target,'SOURCE_ASYNC_RECEIVE_CHANGED'); END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER sa_async_ingress_control_changed AFTER UPDATE
    ON app.source_ingress_controls FOR EACH ROW EXECUTE FUNCTION app.sa_async_ingress_control_changed();

-- Full Source re-read, role/receipt verification, admission, CAS, expiry, decryption,
-- request send and container inspection remain original Engine/runtime work.
-- This DDL never creates a Task/Run, starts a process, retries unknown or refunds fees.
