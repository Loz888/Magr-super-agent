-- SA11 proposal: 0042 is not allocated until a new fixed Harness acknowledgement.
-- Source-native metadata mailbox facts belong to the original Engine connection.
-- Original Task/Run/Mandate and legacy Mailbox/input state stay authoritative.
-- No Source bodies, fake legacy Trigger/Issue, model calls or outbound actions.

CREATE TABLE app.sa_source_subscriptions (
    subscription_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    source_instance_id uuid NOT NULL,
    correlation_id uuid NOT NULL,
    seed_input_id uuid NOT NULL,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision>=0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(subscription_id,project_id,source_instance_id),
    FOREIGN KEY(source_instance_id,project_id)
        REFERENCES app.source_instances(source_instance_id,project_id),
    FOREIGN KEY(correlation_id,project_id)
        REFERENCES app.sa_task_correlations(correlation_id,project_id),
    FOREIGN KEY(seed_input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id)
);

CREATE TABLE app.sa_source_subscription_versions (
    subscription_id uuid NOT NULL,
    revision bigint NOT NULL CHECK (revision>0),
    previous_revision bigint,
    project_id text COLLATE "C" NOT NULL,
    source_instance_id uuid NOT NULL,
    correlation_id uuid NOT NULL,
    correlation_revision bigint NOT NULL CHECK (correlation_revision>0),
    seed_input_id uuid NOT NULL,
    version_fingerprint char(64) NOT NULL CHECK (version_fingerprint ~ '^[a-f0-9]{64}$'),
    thread_id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    run_id uuid NOT NULL,
    mandate_id uuid NOT NULL REFERENCES app.mandates(mandate_id),
    mandate_sha256 char(64) NOT NULL CHECK (mandate_sha256 ~ '^[a-f0-9]{64}$'),
    role text NOT NULL CHECK (role IN ('primary','context_only','observer')),
    state text NOT NULL CHECK (state IN ('active','unsubscribed','handed_back','expired')),
    expires_at timestamptz NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'
        AND octet_length(payload::text)<=16384),
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    review_sha256 char(64) NOT NULL CHECK (review_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    signed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(subscription_id,revision),
    UNIQUE(subscription_id,revision,project_id,source_instance_id),
    FOREIGN KEY(subscription_id,project_id,source_instance_id)
        REFERENCES app.sa_source_subscriptions(subscription_id,project_id,source_instance_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(subscription_id,previous_revision)
        REFERENCES app.sa_source_subscription_versions(subscription_id,revision)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY(correlation_id,project_id,correlation_revision)
        REFERENCES app.sa_task_correlation_versions(correlation_id,project_id,revision),
    FOREIGN KEY(seed_input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id),
    FOREIGN KEY(thread_id,project_id) REFERENCES app.threads(thread_id,project_id),
    FOREIGN KEY(ticket_id,thread_id) REFERENCES app.tickets(ticket_id,thread_id),
    FOREIGN KEY(run_id,ticket_id) REFERENCES app.runs(run_id,ticket_id),
    CHECK ((revision=1 AND previous_revision IS NULL)
        OR (revision>1 AND previous_revision=revision-1))
);
ALTER TABLE app.sa_source_subscriptions ADD CONSTRAINT sa_source_subscription_head_version
    FOREIGN KEY(subscription_id,revision,project_id,source_instance_id)
    REFERENCES app.sa_source_subscription_versions
        (subscription_id,revision,project_id,source_instance_id)
    DEFERRABLE INITIALLY DEFERRED;

-- One minimal routing observation fences duplicate task evaluation, even when
-- a destination queue is full or several signed selectors are ambiguous.
CREATE TABLE app.sa_source_subscription_routes (
    input_id uuid PRIMARY KEY,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    version_fingerprint char(64) NOT NULL CHECK (version_fingerprint ~ '^[a-f0-9]{64}$'),
    outcome text NOT NULL CHECK (outcome IN
        ('delivered','pending','blocked','ambiguous','closed_followup','shadow')),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'
        AND octet_length(payload::text)<=16384),
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id)
);

CREATE TABLE app.sa_source_subscription_deliveries (
    delivery_id uuid PRIMARY KEY,
    subscription_id uuid NOT NULL,
    subscription_revision bigint NOT NULL,
    input_id uuid NOT NULL,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    version_fingerprint char(64) NOT NULL CHECK (version_fingerprint ~ '^[a-f0-9]{64}$'),
    thread_id uuid NOT NULL,
    ticket_id uuid NOT NULL,
    run_id uuid NOT NULL,
    mandate_id uuid NOT NULL REFERENCES app.mandates(mandate_id),
    source_seq bigint NOT NULL CHECK (source_seq>0),
    lane text NOT NULL CHECK (lane IN ('primary','context_only','observer')),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'
        AND octet_length(payload::text)<=16384),
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(subscription_id,input_id),
    UNIQUE(run_id,lane,input_id),
    UNIQUE(run_id,lane,source_seq),
    FOREIGN KEY(subscription_id,subscription_revision,project_id,source_instance_id)
        REFERENCES app.sa_source_subscription_versions
            (subscription_id,revision,project_id,source_instance_id),
    FOREIGN KEY(input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id),
    FOREIGN KEY(thread_id,project_id) REFERENCES app.threads(thread_id,project_id),
    FOREIGN KEY(ticket_id,thread_id) REFERENCES app.tickets(ticket_id,thread_id),
    FOREIGN KEY(run_id,ticket_id) REFERENCES app.runs(run_id,ticket_id)
);
CREATE INDEX sa_source_delivery_pages
    ON app.sa_source_subscription_deliveries(run_id,lane,source_seq);

CREATE TABLE app.sa_source_subscription_intakes (
    snapshot_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    run_id uuid NOT NULL REFERENCES app.runs(run_id),
    lane text NOT NULL CHECK (lane IN ('primary','context_only','observer')),
    execution_epoch bigint NOT NULL CHECK (execution_epoch>=0),
    cursor_from bigint NOT NULL CHECK (cursor_from>=0),
    cursor_to bigint NOT NULL CHECK (cursor_to>cursor_from),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'
        AND octet_length(payload::text)<=65536),
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(snapshot_id,run_id,lane,execution_epoch,cursor_from,cursor_to,payload_sha256),
    UNIQUE(run_id,lane,execution_epoch,cursor_from,payload_sha256)
);

CREATE TABLE app.sa_source_subscription_consumptions (
    snapshot_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    run_id uuid NOT NULL,
    lane text NOT NULL CHECK (lane IN ('primary','context_only','observer')),
    execution_epoch bigint NOT NULL CHECK (execution_epoch>=0),
    cursor_from bigint NOT NULL CHECK (cursor_from>=0),
    cursor_to bigint NOT NULL CHECK (cursor_to>cursor_from),
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    acknowledged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(run_id,lane,cursor_from),
    FOREIGN KEY(snapshot_id,run_id,lane,execution_epoch,cursor_from,cursor_to,payload_sha256)
        REFERENCES app.sa_source_subscription_intakes
            (snapshot_id,run_id,lane,execution_epoch,cursor_from,cursor_to,payload_sha256)
);

CREATE TABLE app.sa_source_subscription_handbacks (
    handback_id uuid PRIMARY KEY,
    subscription_id uuid NOT NULL,
    subscription_revision bigint NOT NULL,
    input_id uuid NOT NULL,
    source_instance_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    version_fingerprint char(64) NOT NULL CHECK (version_fingerprint ~ '^[a-f0-9]{64}$'),
    loop_key char(64) NOT NULL CHECK (loop_key ~ '^[a-f0-9]{64}$'),
    outcome text NOT NULL CHECK (outcome IN ('needs_routing','loop_blocked')),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'
        AND octet_length(payload::text)<=16384),
    payload_sha256 char(64) NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    review_sha256 char(64) NOT NULL CHECK (review_sha256 ~ '^[a-f0-9]{64}$'),
    signed_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(subscription_id,input_id,version_fingerprint),
    FOREIGN KEY(subscription_id,subscription_revision,project_id,source_instance_id)
        REFERENCES app.sa_source_subscription_versions
            (subscription_id,revision,project_id,source_instance_id),
    FOREIGN KEY(input_id,source_instance_id,project_id)
        REFERENCES app.source_ingress_inputs(input_id,source_instance_id,project_id)
);
CREATE INDEX sa_source_handback_window
    ON app.sa_source_subscription_handbacks(project_id,loop_key,created_at);

CREATE FUNCTION app.sa_source_subscription_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'source subscription facts are immutable; append reviewed facts';
END $$;
CREATE TRIGGER sa_source_subscription_version_immutable BEFORE UPDATE OR DELETE
    ON app.sa_source_subscription_versions FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_immutable();
CREATE TRIGGER sa_source_subscription_route_immutable BEFORE UPDATE OR DELETE
    ON app.sa_source_subscription_routes FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_immutable();
CREATE TRIGGER sa_source_subscription_delivery_immutable BEFORE UPDATE OR DELETE
    ON app.sa_source_subscription_deliveries FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_immutable();
CREATE TRIGGER sa_source_subscription_intake_immutable BEFORE UPDATE OR DELETE
    ON app.sa_source_subscription_intakes FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_immutable();
CREATE TRIGGER sa_source_subscription_consumption_immutable BEFORE UPDATE OR DELETE
    ON app.sa_source_subscription_consumptions FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_immutable();
CREATE TRIGGER sa_source_subscription_handback_immutable BEFORE UPDATE OR DELETE
    ON app.sa_source_subscription_handbacks FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_immutable();

CREATE FUNCTION app.sa_source_subscription_head_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'source subscription identity cannot disappear';
    ELSIF TG_OP='INSERT' THEN
        IF NEW.revision<>0 THEN RAISE EXCEPTION 'source subscription begins with an uncommitted zero head'; END IF;
    ELSIF ROW(NEW.subscription_id,NEW.project_id,NEW.source_instance_id,NEW.correlation_id,
        NEW.seed_input_id,NEW.created_at) IS DISTINCT FROM
        ROW(OLD.subscription_id,OLD.project_id,OLD.source_instance_id,OLD.correlation_id,
        OLD.seed_input_id,OLD.created_at) OR NEW.revision<>OLD.revision+1 THEN
        RAISE EXCEPTION 'source subscription identity is fixed; revision must advance once';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_subscription_head_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_source_subscriptions FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_head_guard();

CREATE FUNCTION app.sa_source_subscription_canonical_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE identifier uuid;
BEGIN
    identifier := CASE WHEN TG_TABLE_NAME='sa_source_subscription_versions'
        THEN NEW.seed_input_id ELSE NEW.input_id END;
    IF NOT EXISTS (SELECT 1 FROM app.source_ingress_inputs i WHERE i.input_id=identifier
        AND i.source_instance_id=NEW.source_instance_id AND i.project_id=NEW.project_id
        AND i.version_fingerprint=NEW.version_fingerprint) THEN
        RAISE EXCEPTION 'source subscription requires the exact permanent canonical input';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_subscription_version_canonical BEFORE INSERT
    ON app.sa_source_subscription_versions FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_canonical_guard();
CREATE TRIGGER sa_source_subscription_route_canonical BEFORE INSERT
    ON app.sa_source_subscription_routes FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_canonical_guard();
CREATE TRIGGER sa_source_subscription_delivery_canonical BEFORE INSERT
    ON app.sa_source_subscription_deliveries FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_canonical_guard();
CREATE TRIGGER sa_source_subscription_handback_canonical BEFORE INSERT
    ON app.sa_source_subscription_handbacks FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_canonical_guard();

CREATE FUNCTION app.sa_source_subscription_target_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM app.threads th JOIN app.tickets t USING(thread_id)
        JOIN app.runs r USING(ticket_id) WHERE th.thread_id=NEW.thread_id
        AND th.project_id=NEW.project_id AND t.ticket_id=NEW.ticket_id
        AND t.mandate_id=NEW.mandate_id AND r.run_id=NEW.run_id) THEN
        RAISE EXCEPTION 'source subscription target must be the original scoped Task/Run/Mandate';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_subscription_version_target BEFORE INSERT
    ON app.sa_source_subscription_versions FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_target_guard();
CREATE TRIGGER sa_source_subscription_delivery_target BEFORE INSERT
    ON app.sa_source_subscription_deliveries FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_target_guard();

CREATE FUNCTION app.sa_source_subscription_cursor_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prior bigint;
BEGIN
    SELECT coalesce(max(cursor_to),0) INTO prior FROM app.sa_source_subscription_consumptions
        WHERE run_id=NEW.run_id AND lane=NEW.lane;
    IF NEW.cursor_from<>prior THEN
        RAISE EXCEPTION 'source consumption must acknowledge the current contiguous lane cursor';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_subscription_cursor_guard BEFORE INSERT
    ON app.sa_source_subscription_consumptions FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_cursor_guard();

CREATE FUNCTION app.sa_source_subscription_version_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE h record; prior record;
BEGIN
    SELECT * INTO h FROM app.sa_source_subscriptions WHERE subscription_id=NEW.subscription_id;
    IF NOT FOUND OR ROW(NEW.project_id,NEW.source_instance_id,NEW.correlation_id)
        IS DISTINCT FROM ROW(h.project_id,h.source_instance_id,h.correlation_id) THEN
        RAISE EXCEPTION 'subscription version must preserve its scoped head identity';
    END IF;
    IF NEW.revision>1 THEN
        SELECT * INTO prior FROM app.sa_source_subscription_versions
            WHERE subscription_id=NEW.subscription_id AND revision=NEW.previous_revision;
        IF NOT FOUND OR ROW(NEW.thread_id,NEW.ticket_id,NEW.mandate_id,NEW.mandate_sha256,NEW.role)
            IS DISTINCT FROM ROW(prior.thread_id,prior.ticket_id,prior.mandate_id,
                prior.mandate_sha256,prior.role) THEN
            RAISE EXCEPTION 'new subscription revision cannot replace the Task, mandate or role';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_subscription_version_guard BEFORE INSERT
    ON app.sa_source_subscription_versions FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_version_guard();

CREATE FUNCTION app.sa_source_subscription_delivery_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM app.sa_source_subscription_versions v
        JOIN app.threads th ON th.thread_id=v.thread_id
        WHERE v.subscription_id=NEW.subscription_id AND v.revision=NEW.subscription_revision
        AND v.state='active' AND v.project_id=NEW.project_id
        AND v.source_instance_id=NEW.source_instance_id AND v.thread_id=NEW.thread_id
        AND v.ticket_id=NEW.ticket_id AND v.run_id=NEW.run_id AND v.mandate_id=NEW.mandate_id
        AND th.latest_seq>=NEW.source_seq
        AND (NEW.lane='observer' OR v.role='primary' OR v.role=NEW.lane)) THEN
        RAISE EXCEPTION 'delivery must preserve the exact subscription version and original sequence';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_subscription_delivery_guard BEFORE INSERT
    ON app.sa_source_subscription_deliveries FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_delivery_guard();

CREATE FUNCTION app.sa_source_subscription_run_scope_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM app.runs r JOIN app.tickets t USING(ticket_id)
        JOIN app.threads th USING(thread_id) WHERE r.run_id=NEW.run_id
        AND th.project_id=NEW.project_id) THEN
        RAISE EXCEPTION 'source intake and acknowledgement belong to the original Run project';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_subscription_intake_run_scope BEFORE INSERT
    ON app.sa_source_subscription_intakes FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_run_scope_guard();
CREATE TRIGGER sa_source_subscription_consumption_run_scope BEFORE INSERT
    ON app.sa_source_subscription_consumptions FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_run_scope_guard();

CREATE FUNCTION app.sa_source_subscription_complete_head() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE h record; maximum bigint; versions bigint;
BEGIN
    SELECT * INTO h FROM app.sa_source_subscriptions WHERE subscription_id=NEW.subscription_id;
    SELECT max(revision),count(*) INTO maximum,versions
        FROM app.sa_source_subscription_versions WHERE subscription_id=NEW.subscription_id;
    IF h.revision<=0 OR h.revision IS DISTINCT FROM maximum OR h.revision<>versions THEN
        RAISE EXCEPTION 'a committed subscription needs a complete current immutable version chain';
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER sa_source_subscription_complete_head
    AFTER INSERT OR UPDATE ON app.sa_source_subscriptions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_complete_head();
CREATE CONSTRAINT TRIGGER sa_source_subscription_complete_history
    AFTER INSERT ON app.sa_source_subscription_versions
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION app.sa_source_subscription_complete_head();
