-- SA17 proposal. Do not install as a formal migration before fixed ownership confirmation.
-- Notification authority/causality/receipts only. Original Action/Outbox owns delivery state.
ALTER TABLE app.actions DROP CONSTRAINT actions_action_type_check;
ALTER TABLE app.actions ADD CONSTRAINT actions_action_type_check
    CHECK(action_type IN ('issue_comment','draft_pr','source_notification'));

CREATE TABLE app.sa_notification_controls (
    source_instance_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL CHECK(revision>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(source_instance_id,project_id),
    FOREIGN KEY(source_instance_id,project_id)
        REFERENCES app.source_instances(source_instance_id,project_id)
);
CREATE TABLE app.sa_notification_versions (
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    revision bigint NOT NULL CHECK(revision>0),
    settings jsonb NOT NULL CHECK(jsonb_typeof(settings)='object'
        AND octet_length(settings::text)<=16384
        AND settings ?& ARRAY['enabled','policy_id','policy_revision','policy_sha256',
            'profile_sha256','destination','content_classes','cooldown_seconds',
            'aggregation_seconds','ttl_seconds','valid_from','valid_until']
        AND settings-ARRAY['enabled','policy_id','policy_revision','policy_sha256',
            'profile_sha256','destination','content_classes','cooldown_seconds',
            'aggregation_seconds','ttl_seconds','valid_from','valid_until']='{}'::jsonb),
    settings_sha256 char(64) NOT NULL CHECK(settings_sha256 ~ '^[a-f0-9]{64}$'),
    policy_id uuid NOT NULL,
    policy_revision bigint NOT NULL,
    profile_sha256 char(64) NOT NULL CHECK(profile_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    review_sha256 char(64) NOT NULL CHECK(review_sha256 ~ '^[a-f0-9]{64}$'),
    signed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(source_instance_id,revision),
    UNIQUE(source_instance_id,revision,project_id),
    FOREIGN KEY(source_instance_id,project_id)
        REFERENCES app.sa_notification_controls(source_instance_id,project_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(policy_id,project_id) REFERENCES app.source_policies(policy_id,project_id),
    FOREIGN KEY(policy_id,policy_revision) REFERENCES app.source_policy_versions(policy_id,revision),
    CHECK(settings->>'policy_id'=policy_id::text
        AND settings->>'policy_revision'=policy_revision::text
        AND settings->>'profile_sha256'=profile_sha256::text)
);
ALTER TABLE app.sa_notification_controls ADD CONSTRAINT sa_notification_published_version
    FOREIGN KEY(source_instance_id,revision,project_id)
        REFERENCES app.sa_notification_versions(source_instance_id,revision,project_id)
        DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE app.sa_notification_intents (
    action_id uuid PRIMARY KEY REFERENCES app.actions(action_id),
    project_id text COLLATE "C" NOT NULL,
    source_instance_id uuid NOT NULL,
    control_revision bigint NOT NULL,
    run_id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    input_id uuid NOT NULL REFERENCES app.source_ingress_inputs(input_id),
    source_ref jsonb NOT NULL CHECK(jsonb_typeof(source_ref)='object'
        AND source_ref ?& ARRAY['input_id','source_instance_id','project_id',
            'version_fingerprint','state_sha256']
        AND source_ref-ARRAY['input_id','source_instance_id','project_id',
            'version_fingerprint','state_sha256']='{}'::jsonb),
    policy_id uuid NOT NULL,
    policy_revision bigint NOT NULL,
    profile_sha256 char(64) NOT NULL CHECK(profile_sha256 ~ '^[a-f0-9]{64}$'),
    destination jsonb NOT NULL CHECK(jsonb_typeof(destination)='object'
        AND octet_length(destination::text)<=4096),
    sender jsonb NOT NULL CHECK(jsonb_typeof(sender)='object' AND octet_length(sender::text)<=2048),
    content_class text NOT NULL CHECK(content_class IN
        ('verified_result','necessary_question','review_available','critical_fault')),
    cause_sha256 char(64) NOT NULL CHECK(cause_sha256 ~ '^[a-f0-9]{64}$'),
    facts_sha256 char(64) NOT NULL CHECK(facts_sha256 ~ '^[a-f0-9]{64}$'),
    body_sha256 char(64) NOT NULL CHECK(body_sha256 ~ '^[a-f0-9]{64}$'),
    payload_sha256 char(64) NOT NULL CHECK(payload_sha256 ~ '^[a-f0-9]{64}$'),
    idempotency_key char(64) NOT NULL CHECK(idempotency_key ~ '^[a-f0-9]{64}$'),
    aggregation_key char(64) NOT NULL CHECK(aggregation_key ~ '^[a-f0-9]{64}$'),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(project_id,idempotency_key),
    UNIQUE(action_id,project_id),
    FOREIGN KEY(run_id,ticket_id) REFERENCES app.runs(run_id,ticket_id),
    FOREIGN KEY(source_instance_id,control_revision,project_id)
        REFERENCES app.sa_notification_versions(source_instance_id,revision,project_id),
    FOREIGN KEY(policy_id,policy_revision) REFERENCES app.source_policy_versions(policy_id,revision),
    CHECK(source_ref->>'input_id'=input_id::text
        AND source_ref->>'project_id'=project_id
        AND source_ref->>'source_instance_id'=source_instance_id::text),
    CHECK(expires_at>created_at AND expires_at<=created_at+interval '1 day')
);
CREATE INDEX sa_notification_origin ON app.sa_notification_intents(project_id,input_id);
CREATE INDEX sa_notification_aggregation ON app.sa_notification_intents(project_id,aggregation_key,created_at);
CREATE TABLE app.sa_notification_receipts (
    receipt_id uuid PRIMARY KEY,
    action_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    phase text NOT NULL CHECK(phase IN ('dispatch','query','recovery')),
    outcome text NOT NULL CHECK(outcome IN ('accepted','rejected','unknown','not_found','unqueryable')),
    remote_message_id text COLLATE "C" CHECK(length(remote_message_id) BETWEEN 1 AND 256
        AND remote_message_id !~ '[[:cntrl:]]'),
    profile_sha256 char(64) NOT NULL CHECK(profile_sha256 ~ '^[a-f0-9]{64}$'),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(action_id,project_id) REFERENCES app.sa_notification_intents(action_id,project_id),
    CHECK((outcome='accepted')=(remote_message_id IS NOT NULL))
);
CREATE UNIQUE INDEX sa_notification_one_dispatch_receipt ON app.sa_notification_receipts(action_id)
    WHERE phase IN ('dispatch','recovery');
CREATE TABLE app.sa_notification_events (
    event_id bigserial PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    source_instance_id uuid NOT NULL,
    action_id uuid,
    kind text NOT NULL CHECK(kind IN ('configured','planned','duplicate','aggregated','deferred',
        'send_authorized','blocked','cancelled','settled','query','recovered')),
    reason_code text NOT NULL CHECK(reason_code ~ '^NOTIFICATION_[A-Z0-9_]{1,80}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(source_instance_id,project_id) REFERENCES app.source_instances(source_instance_id,project_id),
    FOREIGN KEY(action_id,project_id) REFERENCES app.sa_notification_intents(action_id,project_id)
);
CREATE INDEX sa_notification_recent_events ON app.sa_notification_events(project_id,event_id DESC);
CREATE FUNCTION app.sa_notification_head_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'notification configuration cannot be deleted'; END IF;
    IF ROW(NEW.source_instance_id,NEW.project_id,NEW.created_at)
        IS DISTINCT FROM ROW(OLD.source_instance_id,OLD.project_id,OLD.created_at)
        OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION 'notification identity immutable; revision must advance';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_notification_head_guard BEFORE UPDATE OR DELETE ON app.sa_notification_controls
    FOR EACH ROW EXECUTE FUNCTION app.sa_notification_head_guard();
CREATE TRIGGER sa_notification_versions_immutable BEFORE UPDATE OR DELETE ON app.sa_notification_versions
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER sa_notification_intents_immutable BEFORE UPDATE OR DELETE ON app.sa_notification_intents
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER sa_notification_receipts_immutable BEFORE UPDATE OR DELETE ON app.sa_notification_receipts
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();
CREATE TRIGGER sa_notification_events_immutable BEFORE UPDATE OR DELETE ON app.sa_notification_events
    FOR EACH ROW EXECUTE FUNCTION app.protect_source_contract_fact();

CREATE FUNCTION app.sa_notification_intent_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE a app.actions%ROWTYPE; i app.source_ingress_inputs%ROWTYPE;
BEGIN
    SELECT * INTO a FROM app.actions WHERE action_id=NEW.action_id;
    SELECT * INTO i FROM app.source_ingress_inputs WHERE input_id=NEW.input_id;
    IF a.action_type IS DISTINCT FROM 'source_notification'
        OR a.run_id IS DISTINCT FROM NEW.run_id OR a.payload_hash IS DISTINCT FROM NEW.payload_sha256::text
        OR a.expires_at IS DISTINCT FROM NEW.expires_at
        OR i.project_id IS DISTINCT FROM NEW.project_id
        OR i.source_instance_id IS DISTINCT FROM NEW.source_instance_id
        OR i.policy_id IS DISTINCT FROM NEW.policy_id OR i.policy_revision IS DISTINCT FROM NEW.policy_revision
        OR a.payload->>'notification_version' IS DISTINCT FROM 'source-notification-v1'
        OR a.payload->>'body_sha256' IS DISTINCT FROM NEW.body_sha256::text
        OR a.payload->>'idempotency_key' IS DISTINCT FROM NEW.idempotency_key::text
        OR a.payload->>'cause_sha256' IS DISTINCT FROM NEW.cause_sha256::text
        OR a.payload->>'content_class' IS DISTINCT FROM NEW.content_class
        OR a.payload-ARRAY['notification_version','body','template','content_class','body_sha256',
            'cause_sha256','destination_sha256','idempotency_key']<>'{}'::jsonb
        OR octet_length(a.payload::text)>4096 THEN
        RAISE EXCEPTION 'notification needs exact original Action and Source provenance';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_notification_intent_guard BEFORE INSERT ON app.sa_notification_intents
    FOR EACH ROW EXECUTE FUNCTION app.sa_notification_intent_guard();

-- Revocation invalidates unsent Actions in the SAME original authorizing transaction.
-- Never touch dispatching/unknown receipts or any task, fee or unknown reservation.
CREATE FUNCTION app.sa_notification_cancel(target_input uuid, target_source uuid, reason text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    UPDATE app.actions a SET status='cancelled',reason_code=reason
        FROM app.sa_notification_intents n WHERE a.action_id=n.action_id
        AND (target_input IS NULL OR n.input_id=target_input)
        AND (target_source IS NULL OR n.source_instance_id=target_source)
        AND a.status IN ('proposed','approved','ready');
    UPDATE app.outbox o SET status='cancelled' FROM app.actions a
        JOIN app.sa_notification_intents n USING(action_id) WHERE o.action_id=a.action_id
        AND a.status='cancelled' AND o.status='ready'
        AND (target_input IS NULL OR n.input_id=target_input)
        AND (target_source IS NULL OR n.source_instance_id=target_source);
END $$;
CREATE FUNCTION app.sa_notification_source_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM app.sa_notification_cancel(NULL,NEW.source_instance_id,'NOTIFICATION_SOURCE_CHANGED');
    RETURN NULL;
END $$;
CREATE TRIGGER sa_notification_source_changed AFTER INSERT ON app.source_instance_versions
    FOR EACH ROW EXECUTE FUNCTION app.sa_notification_source_changed();
CREATE TRIGGER sa_notification_policy_changed AFTER UPDATE ON app.source_policies
    FOR EACH ROW EXECUTE FUNCTION app.sa_notification_source_changed();
CREATE TRIGGER sa_notification_control_changed AFTER UPDATE ON app.sa_notification_controls
    FOR EACH ROW EXECUTE FUNCTION app.sa_notification_source_changed();
CREATE FUNCTION app.sa_notification_input_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.unavailable_at IS NOT NULL OR NEW.metadata IS NULL THEN
        PERFORM app.sa_notification_cancel(NEW.input_id,NULL,'NOTIFICATION_CONTENT_UNAVAILABLE');
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER sa_notification_input_changed AFTER UPDATE ON app.source_ingress_inputs
    FOR EACH ROW EXECUTE FUNCTION app.sa_notification_input_changed();
CREATE FUNCTION app.sa_notification_identity_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid;
BEGIN
    FOR target IN SELECT input_id FROM app.source_ingress_inputs WHERE identity_id=NEW.identity_id
    LOOP PERFORM app.sa_notification_cancel(target,NULL,'NOTIFICATION_IDENTITY_CHANGED'); END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER sa_notification_identity_changed AFTER UPDATE ON app.source_identities
    FOR EACH ROW EXECUTE FUNCTION app.sa_notification_identity_changed();
CREATE TRIGGER sa_notification_roles_changed AFTER UPDATE ON app.source_role_bindings
    FOR EACH ROW EXECUTE FUNCTION app.sa_notification_identity_changed();
COMMENT ON TABLE app.sa_notification_intents IS
    'SA17 immutable policy approval/causal metadata; original Action/Outbox owns delivery state';
COMMENT ON TABLE app.sa_notification_receipts IS
    'SA17 actual channel observations; absence is never permission to resend unknown writes';
