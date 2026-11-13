-- SA12 review draft: 0044 is proposed, not allocated or installed.
-- Original Run/epoch, Engine lock/connection and SA07 quotas remain authority.
-- These are bounded resource/queue facts, never a second Task state machine.
-- No source body, command, credential, model result or outbound payload is stored.

CREATE TABLE app.sa_scheduler_configs (
    project_id text COLLATE "C" PRIMARY KEY CHECK (length(project_id) BETWEEN 1 AND 200),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision>=0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE app.sa_scheduler_config_versions (
    project_id text COLLATE "C" NOT NULL REFERENCES app.sa_scheduler_configs(project_id)
        DEFERRABLE INITIALLY DEFERRED,
    revision bigint NOT NULL CHECK (revision>0),
    previous_revision bigint,
    enabled boolean NOT NULL DEFAULT false,
    active_slots integer NOT NULL DEFAULT 0 CHECK (active_slots BETWEEN 0 AND 2),
    preparing_slots integer NOT NULL DEFAULT 0 CHECK (preparing_slots BETWEEN 0 AND 2),
    total_cpu_millis integer NOT NULL DEFAULT 0 CHECK (total_cpu_millis BETWEEN 0 AND 4000),
    total_memory_mib integer NOT NULL DEFAULT 0 CHECK (total_memory_mib BETWEEN 0 AND 4096),
    max_queue integer NOT NULL DEFAULT 0 CHECK (max_queue BETWEEN 0 AND 256),
    per_source_slots integer NOT NULL DEFAULT 0 CHECK (per_source_slots BETWEEN 0 AND 2),
    lease_seconds integer NOT NULL DEFAULT 30 CHECK (lease_seconds BETWEEN 30 AND 120),
    preparation_seconds integer NOT NULL DEFAULT 120 CHECK (preparation_seconds BETWEEN 1 AND 600),
    session_ttl_seconds integer NOT NULL DEFAULT 3600
        CHECK (session_ttl_seconds BETWEEN 1 AND 86400),
    valid_from timestamptz NOT NULL,
    valid_until timestamptz NOT NULL CHECK (valid_until>valid_from),
    configuration_sha256 char(64) NOT NULL CHECK (configuration_sha256 ~ '^[a-f0-9]{64}$'),
    review_sha256 char(64) NOT NULL CHECK (review_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    signed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(project_id,revision),
    FOREIGN KEY(project_id,previous_revision)
        REFERENCES app.sa_scheduler_config_versions(project_id,revision)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK ((revision=1 AND previous_revision IS NULL)
        OR (revision>1 AND previous_revision=revision-1)),
    CHECK (preparing_slots<=active_slots AND per_source_slots<=active_slots)
);
ALTER TABLE app.sa_scheduler_configs ADD CONSTRAINT sa_scheduler_config_head
    FOREIGN KEY(project_id,revision)
    REFERENCES app.sa_scheduler_config_versions(project_id,revision)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE app.sa_scheduler_queue (
    queue_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    request_id uuid NOT NULL,
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    generation bigint NOT NULL CHECK (generation>0),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    thread_id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    run_id uuid NOT NULL,
    mandate_id uuid NOT NULL REFERENCES app.mandates(mandate_id),
    expected_epoch bigint NOT NULL CHECK (expected_epoch>=0),
    target_sha256 char(64) NOT NULL CHECK (target_sha256 ~ '^[a-f0-9]{64}$'),
    cpu_millis integer NOT NULL CHECK (cpu_millis BETWEEN 100 AND 2000),
    memory_mib integer NOT NULL CHECK (memory_mib BETWEEN 64 AND 2048),
    source_instance_id uuid,
    input_id uuid,
    version_fingerprint char(64) CHECK (version_fingerprint ~ '^[a-f0-9]{64}$'),
    source_state_sha256 char(64) CHECK (source_state_sha256 ~ '^[a-f0-9]{64}$'),
    classification text NOT NULL CHECK (classification IN ('human','automation')),
    fairness_key char(64) NOT NULL CHECK (fairness_key ~ '^[a-f0-9]{64}$'),
    state text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued','claimed','closed')),
    assignment_id uuid,
    queue_quota_key text COLLATE "C" NOT NULL CHECK (length(queue_quota_key) BETWEEN 1 AND 256),
    requested_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    review_sha256 char(64) NOT NULL CHECK (review_sha256 ~ '^[a-f0-9]{64}$'),
    reason_code text CHECK (reason_code ~ '^[A-Z0-9_]{1,100}$'),
    enqueued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claimed_at timestamptz,
    closed_at timestamptz,
    UNIQUE(project_id,request_id),
    UNIQUE(project_id,queue_quota_key),
    UNIQUE(run_id,generation),
    UNIQUE(queue_id,project_id),
    UNIQUE(queue_id,project_id,thread_id,ticket_id,run_id,mandate_id),
    FOREIGN KEY(thread_id,project_id) REFERENCES app.threads(thread_id,project_id),
    FOREIGN KEY(ticket_id,thread_id) REFERENCES app.tickets(ticket_id,thread_id),
    FOREIGN KEY(run_id,ticket_id) REFERENCES app.runs(run_id,ticket_id),
    FOREIGN KEY(source_instance_id,project_id)
        REFERENCES app.source_instances(source_instance_id,project_id),
    FOREIGN KEY(input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id),
    CHECK (num_nonnulls(source_instance_id,input_id,version_fingerprint,source_state_sha256)
        IN (0,4)),
    CHECK (claimed_at IS NULL OR claimed_at>=enqueued_at),
    CHECK (closed_at IS NULL OR (closed_at>=enqueued_at
        AND (claimed_at IS NULL OR closed_at>=claimed_at))),
    CHECK ((state='queued' AND assignment_id IS NULL AND claimed_at IS NULL AND closed_at IS NULL)
        OR (state='claimed' AND assignment_id IS NOT NULL AND claimed_at IS NOT NULL
            AND closed_at IS NULL)
        OR (state='closed' AND closed_at IS NOT NULL))
);
CREATE UNIQUE INDEX sa_scheduler_one_open_queue ON app.sa_scheduler_queue(project_id,run_id)
    WHERE state IN ('queued','claimed');
CREATE INDEX sa_scheduler_fair_queue
    ON app.sa_scheduler_queue(project_id,state,classification,fairness_key,enqueued_at,queue_id);

CREATE TABLE app.sa_scheduler_sessions (
    session_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL CHECK (length(project_id) BETWEEN 1 AND 200),
    request_id uuid NOT NULL,
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    token_sha256 char(64) NOT NULL CHECK (token_sha256 ~ '^[a-f0-9]{64}$'),
    profile_sha256 char(64) NOT NULL CHECK (profile_sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL DEFAULT 'registered' CHECK (state IN ('registered','revoked')),
    registered_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    review_sha256 char(64) NOT NULL CHECK (review_sha256 ~ '^[a-f0-9]{64}$'),
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    last_seen_at timestamptz,
    revoked_at timestamptz,
    UNIQUE(session_id,project_id),
    UNIQUE(project_id,request_id),
    UNIQUE(project_id,token_sha256),
    CHECK (expires_at>registered_at AND expires_at<=registered_at+interval '86400 seconds'),
    CHECK (last_seen_at IS NULL OR (last_seen_at>=registered_at AND last_seen_at<expires_at)),
    CHECK (revoked_at IS NULL OR revoked_at>=registered_at),
    CHECK ((state='registered' AND revoked_at IS NULL)
        OR (state='revoked' AND revoked_at IS NOT NULL))
);

CREATE TABLE app.sa_scheduler_assignments (
    assignment_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    request_id uuid NOT NULL,
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    queue_id uuid NOT NULL UNIQUE,
    session_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    run_id uuid NOT NULL,
    mandate_id uuid NOT NULL REFERENCES app.mandates(mandate_id),
    target_sha256 char(64) NOT NULL CHECK (target_sha256 ~ '^[a-f0-9]{64}$'),
    execution_epoch bigint NOT NULL CHECK (execution_epoch>0),
    config_revision bigint NOT NULL CHECK (config_revision>0),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    workspace_id uuid NOT NULL,
    workspace_sha256 char(64) NOT NULL CHECK (workspace_sha256 ~ '^[a-f0-9]{64}$'),
    cpu_millis integer NOT NULL CHECK (cpu_millis BETWEEN 100 AND 2000),
    memory_mib integer NOT NULL CHECK (memory_mib BETWEEN 64 AND 2048),
    state text NOT NULL DEFAULT 'preparing'
        CHECK (state IN ('preparing','active','draining','released')),
    lease_until timestamptz NOT NULL,
    prepare_until timestamptz NOT NULL,
    last_heartbeat_at timestamptz,
    fenced_at timestamptz,
    reason_code text CHECK (reason_code ~ '^[A-Z0-9_]{1,100}$'),
    quiescence_operation_id uuid,
    quiescence_sha256 char(64) CHECK (quiescence_sha256 ~ '^[a-f0-9]{64}$'),
    quiescent_at timestamptz,
    released_at timestamptz,
    slot_quota_key text COLLATE "C" NOT NULL CHECK (length(slot_quota_key) BETWEEN 1 AND 256),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(project_id,request_id),
    UNIQUE(project_id,slot_quota_key),
    UNIQUE(assignment_id,project_id),
    UNIQUE(assignment_id,queue_id,project_id,run_id),
    UNIQUE(assignment_id,project_id,session_id,run_id,execution_epoch),
    FOREIGN KEY(queue_id,project_id,thread_id,ticket_id,run_id,mandate_id)
        REFERENCES app.sa_scheduler_queue
            (queue_id,project_id,thread_id,ticket_id,run_id,mandate_id),
    FOREIGN KEY(session_id,project_id)
        REFERENCES app.sa_scheduler_sessions(session_id,project_id),
    FOREIGN KEY(project_id,config_revision)
        REFERENCES app.sa_scheduler_config_versions(project_id,revision),
    CHECK (lease_until>created_at AND
        ((last_heartbeat_at IS NULL AND lease_until<=created_at+interval '120 seconds')
        OR (last_heartbeat_at IS NOT NULL AND last_heartbeat_at>=created_at
            AND lease_until<=last_heartbeat_at+interval '120 seconds'))),
    CHECK (prepare_until>created_at AND prepare_until<=created_at+interval '600 seconds'),
    CHECK (updated_at>=created_at),
    CHECK (fenced_at IS NULL OR fenced_at>=created_at),
    CHECK (num_nonnulls(quiescence_operation_id,quiescence_sha256,quiescent_at) IN (0,3)),
    CHECK (quiescent_at IS NULL OR (state IN ('draining','released')
        AND fenced_at IS NOT NULL AND quiescent_at>=fenced_at)),
    CHECK ((state IN ('preparing','active') AND fenced_at IS NULL AND released_at IS NULL)
        OR (state='draining' AND fenced_at IS NOT NULL AND released_at IS NULL)
        OR (state='released' AND fenced_at IS NOT NULL AND released_at IS NOT NULL
            AND quiescent_at IS NOT NULL AND quiescent_at>=fenced_at
            AND released_at>=quiescent_at))
);
ALTER TABLE app.sa_scheduler_queue ADD CONSTRAINT sa_scheduler_queue_assignment
    FOREIGN KEY(assignment_id,queue_id,project_id,run_id)
    REFERENCES app.sa_scheduler_assignments(assignment_id,queue_id,project_id,run_id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE UNIQUE INDEX sa_scheduler_occupied_run
    ON app.sa_scheduler_assignments(project_id,run_id) WHERE state<>'released';
CREATE UNIQUE INDEX sa_scheduler_occupied_ticket
    ON app.sa_scheduler_assignments(project_id,ticket_id) WHERE state<>'released';
CREATE UNIQUE INDEX sa_scheduler_occupied_session
    ON app.sa_scheduler_assignments(project_id,session_id) WHERE state<>'released';
CREATE UNIQUE INDEX sa_scheduler_occupied_workspace
    ON app.sa_scheduler_assignments(project_id,workspace_id) WHERE state<>'released';
CREATE INDEX sa_scheduler_assignment_capacity ON app.sa_scheduler_assignments(project_id,state);

CREATE TABLE app.sa_scheduler_operations (
    operation_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    assignment_id uuid NOT NULL,
    session_id uuid NOT NULL,
    run_id uuid NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch>0),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision>0),
    kind text NOT NULL CHECK (kind IN ('prepare','start','job','stop','inspect')),
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL DEFAULT 'requested'
        CHECK (state IN ('requested','running','unknown','known_terminal')),
    provider_operation_id text COLLATE "C"
        CHECK (provider_operation_id ~ '^[A-Za-z0-9_.:-]{1,200}$'),
    result_code text CHECK (result_code IN ('succeeded','failed','cancelled','absent')),
    result_sha256 char(64) CHECK (result_sha256 ~ '^[a-f0-9]{64}$'),
    exit_code integer,
    quiescent boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    observed_at timestamptz,
    UNIQUE(operation_id,project_id),
    UNIQUE(operation_id,assignment_id,project_id),
    UNIQUE(assignment_id,kind,request_sha256),
    FOREIGN KEY(assignment_id,project_id,session_id,run_id,execution_epoch)
        REFERENCES app.sa_scheduler_assignments
            (assignment_id,project_id,session_id,run_id,execution_epoch),
    CHECK (updated_at>=created_at AND (observed_at IS NULL OR observed_at>=created_at)),
    CHECK ((state='known_terminal' AND result_code IS NOT NULL
            AND result_sha256 IS NOT NULL AND observed_at IS NOT NULL)
        OR (state<>'known_terminal' AND result_code IS NULL
            AND result_sha256 IS NULL AND exit_code IS NULL AND NOT quiescent)),
    CHECK (NOT quiescent OR (state='known_terminal' AND kind IN ('stop','inspect')))
);
ALTER TABLE app.sa_scheduler_assignments ADD CONSTRAINT sa_scheduler_quiescence_operation
    FOREIGN KEY(quiescence_operation_id,assignment_id,project_id)
    REFERENCES app.sa_scheduler_operations(operation_id,assignment_id,project_id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE INDEX sa_scheduler_pending_operations
    ON app.sa_scheduler_operations(assignment_id,state) WHERE state<>'known_terminal';

CREATE TABLE app.sa_scheduler_events (
    event_id bigserial PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL CHECK (length(project_id) BETWEEN 1 AND 200),
    event_type text NOT NULL CHECK (event_type ~ '^[A-Z0-9_]{1,100}$'),
    queue_id uuid,
    session_id uuid,
    assignment_id uuid,
    operation_id uuid,
    revision bigint NOT NULL CHECK (revision>0),
    reason_code text CHECK (reason_code ~ '^[A-Z0-9_]{1,100}$'),
    details jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(details)='object'
        AND octet_length(details::text)<=4096
        AND details-ARRAY['previous_state','state','epoch','cpu_millis','memory_mib',
            'lease_until','request_sha256','result_sha256','profile_sha256','config_revision',
            'queue_generation','classification','fairness_key','quiescent']='{}'::jsonb),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(queue_id,project_id) REFERENCES app.sa_scheduler_queue(queue_id,project_id),
    FOREIGN KEY(session_id,project_id) REFERENCES app.sa_scheduler_sessions(session_id,project_id),
    FOREIGN KEY(assignment_id,project_id)
        REFERENCES app.sa_scheduler_assignments(assignment_id,project_id),
    FOREIGN KEY(operation_id,project_id)
        REFERENCES app.sa_scheduler_operations(operation_id,project_id)
);
CREATE INDEX sa_scheduler_event_history ON app.sa_scheduler_events(project_id,event_id);

CREATE FUNCTION app.sa_scheduler_immutable_fact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'scheduler history is immutable; append a new fact';
END $$;
CREATE TRIGGER sa_scheduler_config_version_immutable BEFORE UPDATE OR DELETE
    ON app.sa_scheduler_config_versions FOR EACH ROW EXECUTE FUNCTION app.sa_scheduler_immutable_fact();
CREATE TRIGGER sa_scheduler_event_immutable BEFORE UPDATE OR DELETE
    ON app.sa_scheduler_events FOR EACH ROW EXECUTE FUNCTION app.sa_scheduler_immutable_fact();

CREATE FUNCTION app.sa_scheduler_config_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'scheduler configuration identity is permanent'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>0 THEN RAISE EXCEPTION 'scheduler head begins at zero'; END IF;
    ELSIF ROW(NEW.project_id,NEW.created_at) IS DISTINCT FROM ROW(OLD.project_id,OLD.created_at)
        OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION 'scheduler configuration identity is fixed; revision advances once';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_scheduler_config_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_scheduler_configs FOR EACH ROW EXECUTE FUNCTION app.sa_scheduler_config_guard();

CREATE FUNCTION app.sa_scheduler_queue_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    prior_generation bigint;
    queue_limit integer;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'scheduler queue identity is permanent'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 OR NEW.state<>'queued' THEN
            RAISE EXCEPTION 'scheduler queue begins queued at revision one';
        END IF;
        SELECT coalesce(max(generation),0) INTO prior_generation
            FROM app.sa_scheduler_queue WHERE run_id=NEW.run_id;
        IF NEW.generation<>prior_generation+1 THEN
            RAISE EXCEPTION 'scheduler queue generation advances once for each new admission';
        END IF;
        SELECT v.max_queue INTO queue_limit FROM app.sa_scheduler_configs h
            JOIN app.sa_scheduler_config_versions v USING(project_id,revision)
            WHERE h.project_id=NEW.project_id FOR UPDATE OF h;
        IF queue_limit IS NULL OR (SELECT count(*) FROM app.sa_scheduler_queue
            WHERE project_id=NEW.project_id AND state='queued')>=queue_limit THEN
            RAISE EXCEPTION 'scheduler queue exceeds the current signed bounded capacity';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM app.threads h JOIN app.tickets t USING(thread_id)
            JOIN app.runs r USING(ticket_id) JOIN app.mandates m ON m.mandate_id=t.mandate_id
            WHERE h.thread_id=NEW.thread_id
            AND h.project_id=NEW.project_id AND t.ticket_id=NEW.ticket_id
            AND t.mandate_id=NEW.mandate_id AND m.project_id=NEW.project_id AND r.run_id=NEW.run_id
            AND r.execution_epoch=NEW.expected_epoch AND r.status='queued' AND t.status='open') THEN
            RAISE EXCEPTION 'scheduler queue requires the exact current original queued target';
        END IF;
        IF NEW.input_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM app.source_ingress_inputs i WHERE i.input_id=NEW.input_id
            AND i.source_instance_id=NEW.source_instance_id AND i.project_id=NEW.project_id
            AND i.version_fingerprint=NEW.version_fingerprint) THEN
            RAISE EXCEPTION 'scheduler origin requires the exact permanent canonical input';
        END IF;
    ELSE
        IF NEW.revision<>OLD.revision+1 OR ROW(NEW.queue_id,NEW.project_id,NEW.request_id,
            NEW.request_sha256,NEW.generation,NEW.thread_id,NEW.ticket_id,NEW.run_id,NEW.mandate_id,
            NEW.expected_epoch,NEW.target_sha256,NEW.cpu_millis,NEW.memory_mib,
            NEW.source_instance_id,NEW.input_id,NEW.version_fingerprint,NEW.source_state_sha256,
            NEW.classification,NEW.fairness_key,NEW.queue_quota_key,NEW.requested_by,
            NEW.review_sha256,NEW.enqueued_at) IS DISTINCT FROM
            ROW(OLD.queue_id,OLD.project_id,OLD.request_id,OLD.request_sha256,OLD.generation,
            OLD.thread_id,OLD.ticket_id,OLD.run_id,OLD.mandate_id,OLD.expected_epoch,
            OLD.target_sha256,OLD.cpu_millis,OLD.memory_mib,OLD.source_instance_id,OLD.input_id,
            OLD.version_fingerprint,OLD.source_state_sha256,OLD.classification,OLD.fairness_key,
            OLD.queue_quota_key,OLD.requested_by,OLD.review_sha256,OLD.enqueued_at) THEN
            RAISE EXCEPTION 'scheduler queue identity is fixed; revision advances once';
        END IF;
        IF OLD.state='closed' OR (OLD.state='claimed' AND NEW.state='queued')
            OR (OLD.assignment_id IS NOT NULL AND NEW.assignment_id IS DISTINCT FROM OLD.assignment_id)
            OR (OLD.claimed_at IS NOT NULL AND NEW.claimed_at IS DISTINCT FROM OLD.claimed_at) THEN
            RAISE EXCEPTION 'scheduler queue cannot reopen or move an existing claim';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_scheduler_queue_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_scheduler_queue FOR EACH ROW EXECUTE FUNCTION app.sa_scheduler_queue_guard();

CREATE FUNCTION app.sa_scheduler_session_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'scheduler session identity is permanent'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 OR NEW.state<>'registered' THEN
            RAISE EXCEPTION 'scheduler session begins registered at revision one';
        END IF;
    ELSE
        IF OLD.state='revoked' OR NEW.revision<>OLD.revision+1
            OR ROW(NEW.session_id,NEW.project_id,NEW.request_id,NEW.request_sha256,
                NEW.token_sha256,NEW.profile_sha256,NEW.registered_by,NEW.review_sha256,
                NEW.registered_at,NEW.expires_at) IS DISTINCT FROM
               ROW(OLD.session_id,OLD.project_id,OLD.request_id,OLD.request_sha256,
                OLD.token_sha256,OLD.profile_sha256,OLD.registered_by,OLD.review_sha256,
                OLD.registered_at,OLD.expires_at)
            OR (OLD.last_seen_at IS NOT NULL
                AND (NEW.last_seen_at IS NULL OR NEW.last_seen_at<OLD.last_seen_at)) THEN
            RAISE EXCEPTION 'scheduler session cannot change identity, rewind or revive';
        END IF;
        IF NEW.state='registered' AND OLD.expires_at<=clock_timestamp() THEN
            RAISE EXCEPTION 'expired scheduler session cannot be renewed by heartbeat';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_scheduler_session_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_scheduler_sessions FOR EACH ROW EXECUTE FUNCTION app.sa_scheduler_session_guard();

CREATE FUNCTION app.sa_scheduler_assignment_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    cfg app.sa_scheduler_config_versions%ROWTYPE;
    worker app.sa_scheduler_sessions%ROWTYPE;
    occupied bigint;
    preparing bigint;
    cpu bigint;
    memory bigint;
    source_count bigint;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'scheduler assignment identity is permanent'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 OR NEW.state<>'preparing' THEN
            RAISE EXCEPTION 'scheduler assignment begins preparing at revision one';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM app.sa_scheduler_queue q
            JOIN app.runs r ON r.run_id=q.run_id JOIN app.tickets t ON t.ticket_id=q.ticket_id
            WHERE q.queue_id=NEW.queue_id AND q.project_id=NEW.project_id AND q.state='queued'
            AND q.target_sha256=NEW.target_sha256 AND q.cpu_millis=NEW.cpu_millis
            AND q.memory_mib=NEW.memory_mib AND r.status='running' AND t.status='open'
            AND r.execution_epoch=NEW.execution_epoch
            AND NEW.execution_epoch=q.expected_epoch+1) THEN
            RAISE EXCEPTION 'scheduler claim must use the original start epoch exactly once';
        END IF;
    ELSE
        IF NEW.revision<>OLD.revision+1 OR OLD.state='released'
            OR ROW(NEW.assignment_id,NEW.project_id,NEW.request_id,NEW.request_sha256,
                NEW.queue_id,NEW.session_id,NEW.thread_id,NEW.ticket_id,NEW.run_id,NEW.mandate_id,
                NEW.target_sha256,NEW.execution_epoch,NEW.config_revision,NEW.workspace_id,
                NEW.workspace_sha256,NEW.cpu_millis,NEW.memory_mib,NEW.prepare_until,
                NEW.slot_quota_key,NEW.created_at) IS DISTINCT FROM
               ROW(OLD.assignment_id,OLD.project_id,OLD.request_id,OLD.request_sha256,
                OLD.queue_id,OLD.session_id,OLD.thread_id,OLD.ticket_id,OLD.run_id,OLD.mandate_id,
                OLD.target_sha256,OLD.execution_epoch,OLD.config_revision,OLD.workspace_id,
                OLD.workspace_sha256,OLD.cpu_millis,OLD.memory_mib,OLD.prepare_until,
                OLD.slot_quota_key,OLD.created_at)
            OR NEW.updated_at<OLD.updated_at THEN
            RAISE EXCEPTION 'scheduler assignment identity is fixed; revision advances once';
        END IF;
        IF (OLD.state='preparing' AND NEW.state NOT IN ('preparing','active','draining'))
            OR (OLD.state='active' AND NEW.state NOT IN ('active','draining'))
            OR (OLD.state='draining' AND NEW.state NOT IN ('draining','released')) THEN
            RAISE EXCEPTION 'scheduler assignment cannot reopen or bypass draining';
        END IF;
        IF OLD.fenced_at IS NOT NULL AND NEW.fenced_at IS DISTINCT FROM OLD.fenced_at THEN
            RAISE EXCEPTION 'scheduler fence is immutable once observed';
        END IF;
        IF OLD.quiescent_at IS NOT NULL AND ROW(NEW.quiescence_operation_id,
            NEW.quiescence_sha256,NEW.quiescent_at) IS DISTINCT FROM
            ROW(OLD.quiescence_operation_id,OLD.quiescence_sha256,OLD.quiescent_at) THEN
            RAISE EXCEPTION 'scheduler quiescence proof is immutable once recorded';
        END IF;
        IF OLD.last_heartbeat_at IS NOT NULL AND
            (NEW.last_heartbeat_at IS NULL OR NEW.last_heartbeat_at<OLD.last_heartbeat_at) THEN
            RAISE EXCEPTION 'scheduler heartbeat time cannot rewind';
        END IF;
        IF NEW.lease_until>OLD.lease_until AND
            (OLD.lease_until<=clock_timestamp() OR NEW.state='draining') THEN
            RAISE EXCEPTION 'scheduler heartbeat cannot revive expired or fenced authority';
        END IF;
    END IF;
    IF NEW.state IN ('preparing','active') THEN
        SELECT v.* INTO cfg FROM app.sa_scheduler_configs h
            JOIN app.sa_scheduler_config_versions v USING(project_id,revision)
            WHERE h.project_id=NEW.project_id AND h.revision=NEW.config_revision FOR UPDATE OF h;
        SELECT * INTO worker FROM app.sa_scheduler_sessions
            WHERE session_id=NEW.session_id AND project_id=NEW.project_id FOR UPDATE;
        IF cfg.project_id IS NULL OR NOT cfg.enabled OR cfg.valid_from>clock_timestamp()
            OR cfg.valid_until<=clock_timestamp() OR worker.session_id IS NULL
            OR worker.state<>'registered' OR worker.expires_at<=clock_timestamp()
            OR NEW.lease_until<=clock_timestamp()
            OR (NEW.state='preparing' AND NEW.prepare_until<=clock_timestamp())
            OR NEW.prepare_until>NEW.created_at+make_interval(secs=>cfg.preparation_seconds)
            OR NEW.lease_until>least(cfg.valid_until,worker.expires_at,
                clock_timestamp()+make_interval(secs=>cfg.lease_seconds))
            OR NOT EXISTS (SELECT 1 FROM app.runs WHERE run_id=NEW.run_id
                AND execution_epoch=NEW.execution_epoch AND status='running') THEN
            RAISE EXCEPTION 'scheduler execution lease requires current config, session and Run';
        END IF;
        SELECT count(*),count(*) FILTER (WHERE state IN ('preparing','draining')),
            coalesce(sum(cpu_millis),0),coalesce(sum(memory_mib),0)
            INTO occupied,preparing,cpu,memory FROM app.sa_scheduler_assignments
            WHERE project_id=NEW.project_id AND state<>'released'
            AND assignment_id<>NEW.assignment_id;
        IF occupied+1>cfg.active_slots OR cpu+NEW.cpu_millis>cfg.total_cpu_millis
            OR memory+NEW.memory_mib>cfg.total_memory_mib
            OR (NEW.state='preparing' AND preparing+1>cfg.preparing_slots) THEN
            RAISE EXCEPTION 'scheduler occupied resources exceed the signed hard limits';
        END IF;
        SELECT count(*) INTO source_count FROM app.sa_scheduler_assignments a
            JOIN app.sa_scheduler_queue q ON q.queue_id=a.queue_id AND q.project_id=a.project_id
            WHERE a.project_id=NEW.project_id AND a.state<>'released'
            AND a.assignment_id<>NEW.assignment_id AND q.fairness_key=(
                SELECT fairness_key FROM app.sa_scheduler_queue WHERE queue_id=NEW.queue_id);
        IF source_count+1>cfg.per_source_slots THEN
            RAISE EXCEPTION 'scheduler source occupancy exceeds its signed slot limit';
        END IF;
    END IF;
    IF NEW.state='released' THEN
        IF NOT EXISTS (SELECT 1 FROM app.sa_scheduler_operations o
            WHERE o.operation_id=NEW.quiescence_operation_id
            AND o.assignment_id=NEW.assignment_id AND o.project_id=NEW.project_id
            AND o.state='known_terminal' AND o.quiescent
            AND o.result_sha256=NEW.quiescence_sha256 AND o.observed_at=NEW.quiescent_at
            AND o.observed_at>=NEW.fenced_at)
            OR EXISTS (SELECT 1 FROM app.sa_scheduler_operations o
                WHERE o.assignment_id=NEW.assignment_id AND o.state<>'known_terminal')
            OR EXISTS (SELECT 1 FROM app.read_jobs j WHERE j.run_id=NEW.run_id
                AND j.status IN ('queued','running')) THEN
            RAISE EXCEPTION 'scheduler release requires trusted quiescence and no pending work';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_scheduler_assignment_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_scheduler_assignments FOR EACH ROW EXECUTE FUNCTION app.sa_scheduler_assignment_guard();

CREATE FUNCTION app.sa_scheduler_operation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'scheduler operation identity is permanent'; END IF;
    IF TG_OP='INSERT' THEN
        IF NEW.revision<>1 OR NEW.state<>'requested' THEN
            RAISE EXCEPTION 'scheduler operation begins requested at revision one';
        END IF;
        IF EXISTS (SELECT 1 FROM app.sa_scheduler_assignments a
            WHERE a.assignment_id=NEW.assignment_id AND a.state='released') THEN
            RAISE EXCEPTION 'released scheduler assignment cannot start resource operations';
        END IF;
    ELSE
        IF OLD.state='known_terminal' OR NEW.revision<>OLD.revision+1
            OR ROW(NEW.operation_id,NEW.project_id,NEW.assignment_id,NEW.session_id,NEW.run_id,
                NEW.execution_epoch,NEW.kind,NEW.request_sha256,NEW.created_at) IS DISTINCT FROM
               ROW(OLD.operation_id,OLD.project_id,OLD.assignment_id,OLD.session_id,OLD.run_id,
                OLD.execution_epoch,OLD.kind,OLD.request_sha256,OLD.created_at)
            OR (OLD.provider_operation_id IS NOT NULL
                AND NEW.provider_operation_id IS DISTINCT FROM OLD.provider_operation_id)
            OR NEW.updated_at<OLD.updated_at OR NEW.state='requested' THEN
            RAISE EXCEPTION 'scheduler operation identity is fixed; unknown work is not resent';
        END IF;
    END IF;
    IF NEW.kind IN ('prepare','start','job') AND NEW.state IN ('requested','running') THEN
        IF NOT EXISTS (SELECT 1 FROM app.sa_scheduler_assignments a
            JOIN app.runs r ON r.run_id=a.run_id
            JOIN app.sa_scheduler_sessions s ON s.session_id=a.session_id AND s.project_id=a.project_id
            JOIN app.sa_scheduler_configs h ON h.project_id=a.project_id AND h.revision=a.config_revision
            JOIN app.sa_scheduler_config_versions v ON v.project_id=h.project_id
                AND v.revision=h.revision
            WHERE a.assignment_id=NEW.assignment_id AND a.project_id=NEW.project_id
            AND a.state IN ('preparing','active') AND a.lease_until>clock_timestamp()
            AND (a.state<>'preparing' OR a.prepare_until>clock_timestamp())
            AND r.execution_epoch=a.execution_epoch AND r.status='running'
            AND s.state='registered' AND s.expires_at>clock_timestamp()
            AND v.enabled AND v.valid_from<=clock_timestamp() AND v.valid_until>clock_timestamp()) THEN
            RAISE EXCEPTION 'scheduler resource dispatch requires a current exact live lease';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_scheduler_operation_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_scheduler_operations FOR EACH ROW EXECUTE FUNCTION app.sa_scheduler_operation_guard();
