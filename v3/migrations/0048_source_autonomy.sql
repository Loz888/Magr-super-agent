-- SA16 PROPOSAL ONLY. New fixed ownership/number confirmation is required.
-- Add reference/delegation/claim/tick facts, never a second Task/Run or balance.
-- Business writes continue on the original Engine connection and transaction.
CREATE TABLE app.sa_autonomy_rules (
    rule_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    source_instance_id uuid NOT NULL,
    revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
    last_local_date date,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(rule_id,project_id),
    UNIQUE(rule_id,project_id,source_instance_id),
    FOREIGN KEY(source_instance_id,project_id)
        REFERENCES app.source_instances(source_instance_id,project_id)
);
CREATE TABLE app.sa_autonomy_rule_versions (
    rule_id uuid NOT NULL,
    revision bigint NOT NULL CHECK(revision>0),
    project_id text COLLATE "C" NOT NULL,
    source_instance_id uuid NOT NULL,
    policy_id uuid NOT NULL,
    policy_revision bigint NOT NULL CHECK(policy_revision>0),
    policy_sha256 char(64) NOT NULL CHECK(policy_sha256 ~ '^[a-f0-9]{64}$'),
    rule_sha256 char(64) NOT NULL CHECK(rule_sha256 ~ '^[a-f0-9]{64}$'),
    profile_sha256 char(64) NOT NULL CHECK(profile_sha256 ~ '^[a-f0-9]{64}$'),
    definition jsonb NOT NULL CHECK(jsonb_typeof(definition)='object'
        AND octet_length(definition::text)<=16384
        AND definition ?& ARRAY['source_instance_id','policy_id','policy_revision','policy_sha256',
            'rule_ref','rule_sha256','repository','mode','profile_sha256','objective_sha256',
            'cooldown_seconds','task_ttl_seconds','idle_ttl_seconds','max_active','max_attempts',
            'valid_from','valid_until','schedule']
        AND definition-ARRAY['source_instance_id','policy_id','policy_revision','policy_sha256',
            'rule_ref','rule_sha256','repository','mode','profile_sha256','objective_sha256',
            'cooldown_seconds','task_ttl_seconds','idle_ttl_seconds','max_active','max_attempts',
            'valid_from','valid_until','schedule']='{}'::jsonb),
    definition_sha256 char(64) NOT NULL CHECK(definition_sha256 ~ '^[a-f0-9]{64}$'),
    mode text NOT NULL CHECK(mode IN ('disabled','shadow','auto_investigate','auto_repair')),
    valid_from timestamptz NOT NULL,
    valid_until timestamptz NOT NULL,
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    review_sha256 char(64) NOT NULL CHECK(review_sha256 ~ '^[a-f0-9]{64}$'),
    signed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(rule_id,revision),
    UNIQUE(rule_id,revision,project_id,source_instance_id),
    FOREIGN KEY(rule_id,project_id,source_instance_id)
        REFERENCES app.sa_autonomy_rules(rule_id,project_id,source_instance_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(policy_id,policy_revision) REFERENCES app.source_policy_versions(policy_id,revision),
    CHECK(valid_until>valid_from AND valid_until<=valid_from+interval '7 days'),
    CHECK(definition->>'source_instance_id'=source_instance_id::text
        AND definition->>'policy_id'=policy_id::text
        AND definition->>'policy_revision'=policy_revision::text
        AND definition->>'policy_sha256'=policy_sha256::text
        AND definition->>'rule_sha256'=rule_sha256::text
        AND definition->>'profile_sha256'=profile_sha256::text AND definition->>'mode'=mode)
);
ALTER TABLE app.sa_autonomy_rules ADD CONSTRAINT sa_autonomy_published_rule
    FOREIGN KEY(rule_id,revision,project_id,source_instance_id)
    REFERENCES app.sa_autonomy_rule_versions(rule_id,revision,project_id,source_instance_id)
    DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE app.sa_autonomy_ticks (
    tick_id uuid PRIMARY KEY,
    rule_id uuid NOT NULL,
    rule_revision bigint NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    source_instance_id uuid NOT NULL,
    timezone text COLLATE "C" NOT NULL CHECK(length(timezone) BETWEEN 1 AND 100),
    local_date date NOT NULL,
    scheduled_at timestamptz NOT NULL,
    signal_sha256 char(64) NOT NULL CHECK(signal_sha256 ~ '^[a-f0-9]{64}$'),
    missed_coalesced integer NOT NULL CHECK(missed_coalesced BETWEEN 0 AND 31),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(rule_id,local_date),
    UNIQUE(project_id,signal_sha256),
    UNIQUE(tick_id,rule_id,project_id),
    FOREIGN KEY(rule_id,rule_revision,project_id,source_instance_id)
        REFERENCES app.sa_autonomy_rule_versions(rule_id,revision,project_id,source_instance_id),
    CHECK(scheduled_at<=observed_at)
);
CREATE TABLE app.sa_autonomy_claims (
    claim_id uuid PRIMARY KEY,
    rule_id uuid NOT NULL,
    rule_revision bigint NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    source_instance_id uuid NOT NULL,
    signal_key char(64) NOT NULL CHECK(signal_key ~ '^[a-f0-9]{64}$'),
    subject_key char(64) NOT NULL CHECK(subject_key ~ '^[a-f0-9]{64}$'),
    decision_key char(64) REFERENCES app.sa_source_eval_decisions(decision_key),
    tick_id uuid,
    signal jsonb NOT NULL CHECK(jsonb_typeof(signal)='object' AND octet_length(signal::text)<=16384
        AND signal-ARRAY['key','subject_key','rule_id','rule_revision','source_instance_id',
            'policy_id','policy_revision','policy_sha256','repository','intended_action','origin',
            'inputs','expires_at']='{}'::jsonb),
    signal_sha256 char(64) NOT NULL CHECK(signal_sha256 ~ '^[a-f0-9]{64}$'),
    disposition text NOT NULL CHECK(disposition IN ('ignored','shadow','duplicate','held','admit')),
    reason_code text NOT NULL CHECK(reason_code ~ '^AUTONOMY_[A-Z0-9_]{1,80}$'),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(project_id,signal_key),
    UNIQUE(claim_id,rule_id,project_id),
    FOREIGN KEY(rule_id,rule_revision,project_id,source_instance_id)
        REFERENCES app.sa_autonomy_rule_versions(rule_id,revision,project_id,source_instance_id),
    FOREIGN KEY(tick_id,rule_id,project_id) REFERENCES app.sa_autonomy_ticks(tick_id,rule_id,project_id),
    CHECK(num_nonnulls(decision_key,tick_id)=1)
);
CREATE INDEX sa_autonomy_subject_history ON app.sa_autonomy_claims(project_id,subject_key,created_at);
CREATE TABLE app.sa_autonomy_attempts (
    attempt_id uuid PRIMARY KEY,
    claim_id uuid NOT NULL,
    rule_id uuid NOT NULL,
    rule_revision bigint NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    source_instance_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    run_id uuid NOT NULL UNIQUE,
    mandate_id uuid NOT NULL,
    mandate_sha256 char(64) NOT NULL CHECK(mandate_sha256 ~ '^[a-f0-9]{64}$'),
    original_kind text NOT NULL CHECK(original_kind IN ('readonly','coding')),
    profile_sha256 char(64) NOT NULL CHECK(profile_sha256 ~ '^[a-f0-9]{64}$'),
    budget_profile_sha256 char(64) NOT NULL CHECK(budget_profile_sha256 ~ '^[a-f0-9]{64}$'),
    input_id uuid NOT NULL REFERENCES app.source_ingress_inputs(input_id),
    source_state_sha256 char(64) NOT NULL CHECK(source_state_sha256 ~ '^[a-f0-9]{64}$'),
    identity_id uuid NOT NULL REFERENCES app.source_identities(identity_id),
    identity_revision bigint NOT NULL CHECK(identity_revision>0),
    conversation_key char(64) NOT NULL CHECK(conversation_key ~ '^[a-f0-9]{64}$'),
    role_binding_id uuid NOT NULL REFERENCES app.source_role_bindings(binding_id),
    role_revision bigint NOT NULL CHECK(role_revision>0),
    revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
    control_state text NOT NULL DEFAULT 'active' CHECK(control_state IN ('active','stopped','expired')),
    reason_code text NOT NULL CHECK(reason_code ~ '^AUTONOMY_[A-Z0-9_]{1,80}$'),
    authority_until timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(attempt_id,project_id),
    UNIQUE(claim_id),
    reopened_from uuid REFERENCES app.sa_autonomy_attempts(attempt_id),
    FOREIGN KEY(claim_id,rule_id,project_id) REFERENCES app.sa_autonomy_claims(claim_id,rule_id,project_id),
    FOREIGN KEY(rule_id,rule_revision,project_id,source_instance_id)
        REFERENCES app.sa_autonomy_rule_versions(rule_id,revision,project_id,source_instance_id),
    FOREIGN KEY(thread_id,project_id) REFERENCES app.threads(thread_id,project_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(ticket_id,thread_id) REFERENCES app.tickets(ticket_id,thread_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(run_id,ticket_id) REFERENCES app.runs(run_id,ticket_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(mandate_id) REFERENCES app.mandates(mandate_id) DEFERRABLE INITIALLY DEFERRED,
    CHECK(authority_until>created_at AND updated_at>=created_at)
);
CREATE INDEX sa_autonomy_active_authority ON app.sa_autonomy_attempts(rule_id,authority_until)
    WHERE control_state='active';
CREATE TABLE app.sa_autonomy_events (
    event_id bigserial PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    rule_id uuid NOT NULL,
    claim_id uuid,
    attempt_id uuid,
    tick_id uuid,
    kind text NOT NULL CHECK(kind IN ('rule_saved','rule_disabled','claim','admitted','held',
        'stopped','expired','waiting','original_terminal','tick','reopen','recovered')),
    reason_code text NOT NULL CHECK(reason_code ~ '^AUTONOMY_[A-Z0-9_]{1,80}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(rule_id,project_id) REFERENCES app.sa_autonomy_rules(rule_id,project_id),
    FOREIGN KEY(claim_id,rule_id,project_id) REFERENCES app.sa_autonomy_claims(claim_id,rule_id,project_id),
    FOREIGN KEY(attempt_id,project_id) REFERENCES app.sa_autonomy_attempts(attempt_id,project_id),
    FOREIGN KEY(tick_id,rule_id,project_id) REFERENCES app.sa_autonomy_ticks(tick_id,rule_id,project_id)
);
CREATE FUNCTION app.sa_autonomy_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'autonomy history is immutable'; END $$;
CREATE TRIGGER sa_autonomy_rule_history BEFORE UPDATE OR DELETE ON app.sa_autonomy_rule_versions
    FOR EACH ROW EXECUTE FUNCTION app.sa_autonomy_immutable();
CREATE TRIGGER sa_autonomy_tick_history BEFORE UPDATE OR DELETE ON app.sa_autonomy_ticks
    FOR EACH ROW EXECUTE FUNCTION app.sa_autonomy_immutable();
CREATE TRIGGER sa_autonomy_claim_history BEFORE UPDATE OR DELETE ON app.sa_autonomy_claims
    FOR EACH ROW EXECUTE FUNCTION app.sa_autonomy_immutable();
CREATE TRIGGER sa_autonomy_event_history BEFORE UPDATE OR DELETE ON app.sa_autonomy_events
    FOR EACH ROW EXECUTE FUNCTION app.sa_autonomy_immutable();
CREATE FUNCTION app.sa_autonomy_rule_head_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'rule identity is permanent'; END IF;
    IF TG_OP='UPDATE' AND (ROW(NEW.rule_id,NEW.project_id,NEW.source_instance_id,NEW.created_at)
        IS DISTINCT FROM ROW(OLD.rule_id,OLD.project_id,OLD.source_instance_id,OLD.created_at)
        OR NEW.revision NOT IN (OLD.revision,OLD.revision+1)
        OR (OLD.last_local_date IS NOT NULL AND
            (NEW.last_local_date IS NULL OR NEW.last_local_date<OLD.last_local_date))) THEN
        RAISE EXCEPTION 'rule identity/cursor is monotone';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_autonomy_rule_head_guard BEFORE UPDATE OR DELETE ON app.sa_autonomy_rules
    FOR EACH ROW EXECUTE FUNCTION app.sa_autonomy_rule_head_guard();
CREATE FUNCTION app.sa_autonomy_attempt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v app.sa_autonomy_rule_versions%ROWTYPE;
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'autonomy origin cannot disappear'; END IF;
    IF TG_OP='UPDATE' THEN
        IF (to_jsonb(NEW)-ARRAY['revision','control_state','reason_code','updated_at']) IS DISTINCT FROM
            (to_jsonb(OLD)-ARRAY['revision','control_state','reason_code','updated_at'])
            OR NEW.revision<>OLD.revision+1 OR NEW.updated_at<OLD.updated_at
            OR (OLD.control_state<>'active' AND NEW.control_state<>OLD.control_state) THEN
            RAISE EXCEPTION 'attempt origin and terminal fencing are immutable';
        END IF;
    ELSE
        SELECT x.* INTO v FROM app.sa_autonomy_rule_versions x JOIN app.sa_autonomy_rules h
            ON h.rule_id=x.rule_id AND h.revision=x.revision
            WHERE x.rule_id=NEW.rule_id AND x.revision=NEW.rule_revision AND x.project_id=NEW.project_id;
        IF v.rule_id IS NULL OR v.mode NOT IN ('auto_investigate','auto_repair')
            OR NEW.revision<>1 OR NEW.control_state<>'active' OR NEW.profile_sha256<>v.profile_sha256
            OR NEW.authority_until>v.valid_until
            OR (NEW.original_kind='readonly')<>(v.mode='auto_investigate')
            OR clock_timestamp()<v.valid_from OR clock_timestamp()>=v.valid_until THEN
            RAISE EXCEPTION 'attempt needs a current reviewed delegation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_autonomy_attempt_guard BEFORE INSERT OR UPDATE OR DELETE ON app.sa_autonomy_attempts
    FOR EACH ROW EXECUTE FUNCTION app.sa_autonomy_attempt_guard();
CREATE FUNCTION app.sa_autonomy_run_current(identifier uuid) RETURNS boolean
LANGUAGE sql VOLATILE AS $$
    SELECT NOT EXISTS(SELECT 1 FROM app.sa_autonomy_attempts WHERE run_id=identifier)
        OR EXISTS(SELECT 1 FROM app.sa_autonomy_attempts a
        JOIN app.sa_autonomy_rules h ON h.rule_id=a.rule_id AND h.revision=a.rule_revision
        JOIN app.sa_autonomy_rule_versions v ON v.rule_id=h.rule_id AND v.revision=h.revision
        JOIN app.source_policies p ON p.policy_id=v.policy_id AND p.current_revision=v.policy_revision
        JOIN app.source_policy_versions pv ON pv.policy_id=p.policy_id AND pv.revision=p.current_revision
        JOIN app.source_ingress_inputs i ON i.input_id=a.input_id AND i.project_id=a.project_id
        JOIN app.source_ingress_entities e ON e.entity_key=i.entity_key
        JOIN app.source_ingress_bodies b ON b.input_id=i.input_id
        JOIN app.source_identities identity ON identity.identity_id=a.identity_id
        JOIN app.source_role_bindings roles ON roles.binding_id=a.role_binding_id
        WHERE a.run_id=identifier AND a.control_state='active'
        AND a.authority_until>clock_timestamp() AND v.valid_from<=clock_timestamp()
        AND v.valid_until>clock_timestamp() AND v.mode IN ('auto_investigate','auto_repair')
        AND NOT pv.revoked AND pv.definition_sha256=v.policy_sha256
        AND pv.definition->>'mode'=v.mode AND pv.definition->'receive'->>'enabled'='true'
        AND pv.definition->'receive'->>'model_input'='true'
        AND pv.definition->'admission'->>'enabled'='true'
        AND pv.definition->'admission'->>'rule_sha256'=v.rule_sha256::text
        AND pv.definition->'admission'->>'rule_ref'=v.definition->>'rule_ref'
        AND (pv.definition->>'valid_from')::timestamptz<=clock_timestamp()
        AND (pv.definition->>'valid_until')::timestamptz>clock_timestamp()
        AND pv.definition->'execution'->>'enabled'='true'
        AND pv.definition->'admission'->'request_kinds' ? 'team_rule'
        AND p.project_id=a.project_id AND p.source_instance_id=a.source_instance_id
        AND i.policy_id=v.policy_id AND i.policy_revision=v.policy_revision
        AND i.source_instance_id=a.source_instance_id AND i.identity_id=a.identity_id
        AND i.identity_revision=a.identity_revision AND i.unavailable_at IS NULL
        AND i.metadata_expires_at>clock_timestamp() AND b.expires_at>clock_timestamp()
        AND e.head_input_id=i.input_id AND NOT e.deleted AND NOT e.ambiguous
        AND identity.project_id=a.project_id AND identity.source_instance_id=a.source_instance_id
        AND identity.revision=a.identity_revision AND identity.revoked_at IS NULL
        AND roles.identity_id=a.identity_id AND roles.project_id=a.project_id
        AND roles.conversation_key=a.conversation_key
        AND roles.revision=a.role_revision AND roles.active
        AND roles.roles ? 'context_provider');
$$;
-- Preserve the original Coding predicate/OID and truthful late settlement.
CREATE OR REPLACE FUNCTION app.sa_coding_current(
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
        AND app.sa_autonomy_run_current(b.run_id)
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

