-- SAV1-03. Authorization facts only; existing Engine owns all writes and business state.
CREATE TABLE app.source_policies (
    policy_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    source_instance_id uuid NOT NULL,
    current_revision bigint NOT NULL CHECK (current_revision>0),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (source_instance_id,project_id) REFERENCES app.source_instances(source_instance_id,project_id),
    UNIQUE (policy_id,project_id)
);
CREATE TABLE app.source_policy_versions (
    policy_id uuid NOT NULL REFERENCES app.source_policies,
    revision bigint NOT NULL CHECK (revision>0),
    definition jsonb NOT NULL CHECK (jsonb_typeof(definition)='object'),
    definition_sha256 char(64) NOT NULL CHECK (definition_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    review_ref text COLLATE "C" NOT NULL,
    revoked boolean NOT NULL DEFAULT false,
    signed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (policy_id,revision)
);
ALTER TABLE app.source_policies ADD CONSTRAINT source_policy_current_version
    FOREIGN KEY (policy_id,current_revision) REFERENCES app.source_policy_versions(policy_id,revision)
    DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE app.source_role_bindings ADD CONSTRAINT source_binding_identity_project
    UNIQUE (binding_id,identity_id,project_id);
CREATE TABLE app.source_run_policies (
    run_id uuid PRIMARY KEY REFERENCES app.runs,
    ticket_id uuid NOT NULL,
    thread_id uuid NOT NULL,
    policy_id uuid NOT NULL,
    policy_revision bigint NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    identity_id uuid NOT NULL,
    identity_revision bigint NOT NULL,
    binding_id uuid NOT NULL,
    evidence_id uuid NOT NULL REFERENCES app.source_identity_evidence,
    facts jsonb NOT NULL CHECK (jsonb_typeof(facts)='object'),
    invalidated_at timestamptz,
    reason_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (policy_id,project_id) REFERENCES app.source_policies(policy_id,project_id),
    FOREIGN KEY (run_id,ticket_id) REFERENCES app.runs(run_id,ticket_id),
    FOREIGN KEY (ticket_id,thread_id) REFERENCES app.tickets(ticket_id,thread_id),
    FOREIGN KEY (thread_id,project_id) REFERENCES app.threads(thread_id,project_id),
    FOREIGN KEY (policy_id,policy_revision) REFERENCES app.source_policy_versions(policy_id,revision),
    FOREIGN KEY (binding_id,identity_id,project_id)
        REFERENCES app.source_role_bindings(binding_id,identity_id,project_id),
    CHECK ((invalidated_at IS NULL)=(reason_code IS NULL))
);
CREATE TABLE app.source_notification_authorizations (
    action_id uuid PRIMARY KEY REFERENCES app.actions,
    run_id uuid NOT NULL REFERENCES app.source_run_policies(run_id),
    payload_hash char(64) NOT NULL CHECK (payload_hash ~ '^[a-f0-9]{64}$'),
    body_sha256 char(64) NOT NULL CHECK (body_sha256 ~ '^[a-f0-9]{64}$'),
    destination jsonb NOT NULL CHECK (jsonb_typeof(destination)='object'),
    content_class text NOT NULL CHECK (content_class IN ('verified_result','necessary_question','review_available','critical_fault')),
    data_class text NOT NULL CHECK (data_class IN ('public_summary','private_summary','private_log')),
    authorized_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.source_policy_audit (
    audit_id bigserial PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    policy_id uuid NOT NULL,
    revision bigint,
    actor_user_id uuid REFERENCES app.web_accounts(user_id),
    event text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (details - ARRAY['review_ref','run_id','action_id','reason_code','fenced_runs']='{}'::jsonb)
);
CREATE INDEX source_run_policies_policy ON app.source_run_policies(policy_id,policy_revision);
CREATE INDEX source_run_policies_identity ON app.source_run_policies(identity_id,identity_revision);
CREATE INDEX source_policy_audit_recent ON app.source_policy_audit(project_id,audit_id DESC);
CREATE FUNCTION app.protect_source_policy_head() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'source policy cannot be deleted'; END IF;
    IF ROW(NEW.policy_id,NEW.project_id,NEW.source_instance_id,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.policy_id,OLD.project_id,OLD.source_instance_id,OLD.created_at)
       OR NEW.current_revision<>OLD.current_revision+1 THEN
        RAISE EXCEPTION 'source policy identity is immutable; revision must advance';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER protect_source_policy_head BEFORE UPDATE OR DELETE ON app.source_policies
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_policy_head();
CREATE FUNCTION app.protect_source_run_policy() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'source Run policy association cannot be deleted'; END IF;
    IF ROW(NEW.run_id,NEW.ticket_id,NEW.thread_id,NEW.policy_id,NEW.policy_revision,NEW.project_id,NEW.identity_id,
           NEW.identity_revision,NEW.binding_id,NEW.evidence_id,NEW.facts,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.run_id,OLD.ticket_id,OLD.thread_id,OLD.policy_id,OLD.policy_revision,OLD.project_id,OLD.identity_id,
           OLD.identity_revision,OLD.binding_id,OLD.evidence_id,OLD.facts,OLD.created_at)
       OR OLD.invalidated_at IS NOT NULL OR NEW.invalidated_at IS NULL THEN
        RAISE EXCEPTION 'source Run authority is immutable and cannot be revived';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER protect_source_run_policy BEFORE UPDATE OR DELETE ON app.source_run_policies
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_run_policy();
CREATE FUNCTION app.fence_source_run(target_run uuid, reason text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    UPDATE app.source_run_policies SET invalidated_at=now(),reason_code=reason
        WHERE run_id=target_run AND invalidated_at IS NULL;
    IF NOT FOUND THEN RETURN; END IF;
    UPDATE app.runs SET status='paused',reason_code=reason,execution_epoch=execution_epoch+1
        WHERE run_id=target_run AND status NOT IN ('succeeded','failed','cancelled');
    UPDATE app.worker_heartbeats SET expires_at=least(expires_at,clock_timestamp()) WHERE run_id=target_run;
    DELETE FROM app.tool_grants WHERE run_id=target_run;
    UPDATE app.read_jobs SET status='cancelled',lease_token=NULL
        WHERE run_id=target_run AND status='queued';
    -- Running read jobs keep only their receipt token; late results cannot resume execution.
    UPDATE app.target_write_grants SET active=false WHERE run_id=target_run AND active;
    UPDATE app.actions SET status='cancelled',reason_code=reason
        WHERE run_id=target_run AND status IN ('proposed','approved','ready');
    UPDATE app.outbox SET status='cancelled' WHERE status='ready' AND action_id IN
        (SELECT action_id FROM app.actions WHERE run_id=target_run AND status='cancelled');
    -- dispatching/outcome_unknown, usage_calls and all financial ledgers stay untouched.
    INSERT INTO app.run_events(run_id,kind,safe_payload)
        VALUES (target_run,'source_authority_fenced',jsonb_build_object('reason_code',reason));
END $$;
CREATE FUNCTION app.fence_changed_source_policy() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_run uuid;
BEGIN
    FOR target_run IN SELECT run_id FROM app.source_run_policies
        WHERE policy_id=NEW.policy_id AND invalidated_at IS NULL
          AND policy_revision<>NEW.current_revision
    LOOP PERFORM app.fence_source_run(target_run,'SOURCE_POLICY_CHANGED'); END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER fence_changed_source_policy AFTER UPDATE ON app.source_policies
    FOR EACH ROW EXECUTE FUNCTION app.fence_changed_source_policy();
CREATE FUNCTION app.fence_changed_source_identity() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_run uuid;
BEGIN
    FOR target_run IN SELECT run_id FROM app.source_run_policies
        WHERE identity_id=NEW.identity_id AND invalidated_at IS NULL
          AND identity_revision<>NEW.revision
    LOOP PERFORM app.fence_source_run(target_run,'SOURCE_IDENTITY_CHANGED'); END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER fence_changed_source_identity AFTER UPDATE ON app.source_identities
    FOR EACH ROW EXECUTE FUNCTION app.fence_changed_source_identity();
CREATE FUNCTION app.fence_changed_source_installation() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_run uuid;
BEGIN
    FOR target_run IN SELECT b.run_id FROM app.source_run_policies b
        JOIN app.source_policies p USING(policy_id)
        WHERE p.source_instance_id=NEW.source_instance_id AND b.invalidated_at IS NULL
    LOOP PERFORM app.fence_source_run(target_run,'SOURCE_CONFIGURATION_CHANGED'); END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER fence_changed_source_installation AFTER INSERT ON app.source_instance_versions
    FOR EACH ROW EXECUTE FUNCTION app.fence_changed_source_installation();
CREATE TRIGGER source_policy_versions_immutable BEFORE UPDATE OR DELETE ON app.source_policy_versions
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER source_notification_authorizations_immutable BEFORE UPDATE OR DELETE ON app.source_notification_authorizations
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER source_policy_audit_immutable BEFORE UPDATE OR DELETE ON app.source_policy_audit
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
