-- SA18 PROPOSAL ONLY. 0050 is not allocated or installed before new agreement.
-- Original Engine, Action/Approval/Outbox and repair_publication_steps remain
-- the only business writer, effect state and three-step publication journal.
-- These six tables contain review/authority/receipt facts, not another Task.

CREATE TABLE app.sa_source_reviews (
    review_id uuid PRIMARY KEY,
    project_id text COLLATE "C" NOT NULL,
    request_id uuid NOT NULL,
    request_sha256 char(64) NOT NULL CHECK (request_sha256 ~ '^[a-f0-9]{64}$'),
    coding_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    verification_id uuid NOT NULL UNIQUE REFERENCES app.sa_coding_verifications(verification_id),
    run_id uuid NOT NULL REFERENCES app.runs(run_id),
    action_id uuid NOT NULL UNIQUE REFERENCES app.actions(action_id),
    prepared_by uuid NOT NULL REFERENCES app.web_accounts(user_id),
    profile_sha256 char(64) NOT NULL CHECK (profile_sha256 ~ '^[a-f0-9]{64}$'),
    context_sha256 char(64) NOT NULL CHECK (context_sha256 ~ '^[a-f0-9]{64}$'),
    bundle_sha256 char(64) NOT NULL CHECK (bundle_sha256 ~ '^[a-f0-9]{64}$'),
    action_sha256 char(64) NOT NULL CHECK (action_sha256 ~ '^[a-f0-9]{64}$'),
    context jsonb NOT NULL CHECK (jsonb_typeof(context)='object'
        AND octet_length(context::text)<=24576
        AND context ?& ARRAY['review_id','coding_id','target','origin','attempt_id',
            'assignment_id','session_id','workspace_id','epoch','input_revision',
            'async_revision','async_input_revision','mandate_sha256','coding_revision',
            'worker_profile_sha256','profile_sha256','environment_sha256','acceptance_sha256',
            'baseline_tree_sha256','registration_sha256','context_sha256','publication','verification','actor',
            'prepared_by','prepared_at','expires_at']
        AND context-ARRAY['review_id','coding_id','target','origin','attempt_id',
            'assignment_id','session_id','workspace_id','epoch','input_revision',
            'async_revision','async_input_revision','mandate_sha256','coding_revision',
            'worker_profile_sha256','profile_sha256','environment_sha256','acceptance_sha256',
            'baseline_tree_sha256','registration_sha256','context_sha256','publication','verification','actor',
            'prepared_by','prepared_at','expires_at']='{}'::jsonb),
    publication_metadata jsonb NOT NULL CHECK (jsonb_typeof(publication_metadata)='object'
        AND octet_length(publication_metadata::text)<=131072
        AND publication_metadata ?& ARRAY['repository','base_branch','base_sha','branch',
            'git_tree','git_blobs','title_sha256','body_sha256','diff_sha256','bundle_sha256']
        AND publication_metadata-ARRAY['repository','base_branch','base_sha','branch',
            'git_tree','git_blobs','title_sha256','body_sha256','diff_sha256','bundle_sha256']='{}'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    invalidated_at timestamptz,
    reason_code text CHECK (reason_code ~ '^SOURCE_REVIEW_[A-Z0-9_]{1,80}$'),
    UNIQUE(review_id,project_id),
    UNIQUE(project_id,request_id),
    FOREIGN KEY(coding_id,project_id,run_id)
        REFERENCES app.sa_coding_bindings(coding_id,project_id,run_id),
    FOREIGN KEY(attempt_id,project_id) REFERENCES app.sa_coding_attempts(attempt_id,project_id),
    CHECK (expires_at>created_at AND expires_at<=created_at+interval '1 hour'),
    CHECK ((invalidated_at IS NULL AND reason_code IS NULL)
        OR (invalidated_at>=created_at AND reason_code IS NOT NULL))
);

CREATE TABLE app.sa_source_review_payloads (
    review_id uuid PRIMARY KEY REFERENCES app.sa_source_reviews(review_id),
    bundle_sha256 char(64) NOT NULL CHECK (bundle_sha256 ~ '^[a-f0-9]{64}$'),
    content_sha256 char(64) NOT NULL CHECK (content_sha256 ~ '^[a-f0-9]{64}$'),
    cipher_sha256 char(64) NOT NULL CHECK (cipher_sha256 ~ '^[a-f0-9]{64}$'),
    ciphertext bytea CHECK (octet_length(ciphertext) BETWEEN 1 AND 1500000),
    plaintext_bytes integer NOT NULL CHECK (plaintext_bytes BETWEEN 1 AND 1048576),
    expires_at timestamptz NOT NULL,
    unavailable_at timestamptz,
    reason_code text CHECK (reason_code ~ '^SOURCE_REVIEW_[A-Z0-9_]{1,80}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((ciphertext IS NOT NULL AND unavailable_at IS NULL AND reason_code IS NULL)
        OR (ciphertext IS NULL AND unavailable_at IS NOT NULL AND reason_code IS NOT NULL))
);

CREATE TABLE app.sa_source_review_presentations (
    presentation_id uuid PRIMARY KEY,
    review_id uuid NOT NULL REFERENCES app.sa_source_reviews(review_id),
    user_id uuid NOT NULL REFERENCES app.web_accounts(user_id),
    session_sha256 char(64) NOT NULL CHECK (session_sha256 ~ '^[a-f0-9]{64}$'),
    context_sha256 char(64) NOT NULL CHECK (context_sha256 ~ '^[a-f0-9]{64}$'),
    bundle_sha256 char(64) NOT NULL CHECK (bundle_sha256 ~ '^[a-f0-9]{64}$'),
    action_sha256 char(64) NOT NULL CHECK (action_sha256 ~ '^[a-f0-9]{64}$'),
    presented_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    CHECK (expires_at>presented_at AND expires_at<=presented_at+interval '120 seconds')
);

CREATE TABLE app.sa_source_review_approvals (
    review_id uuid PRIMARY KEY REFERENCES app.sa_source_reviews(review_id),
    presentation_id uuid NOT NULL UNIQUE REFERENCES app.sa_source_review_presentations(presentation_id),
    approval_id uuid NOT NULL UNIQUE REFERENCES app.approvals(approval_id),
    user_id uuid NOT NULL REFERENCES app.web_accounts(user_id),
    session_sha256 char(64) NOT NULL CHECK (session_sha256 ~ '^[a-f0-9]{64}$'),
    bundle_sha256 char(64) NOT NULL CHECK (bundle_sha256 ~ '^[a-f0-9]{64}$'),
    action_sha256 char(64) NOT NULL CHECK (action_sha256 ~ '^[a-f0-9]{64}$'),
    publication_approval jsonb NOT NULL CHECK (jsonb_typeof(publication_approval)='object'
        AND octet_length(publication_approval::text)<=4096
        AND publication_approval ?& ARRAY['approval_id','actor','repository','base_sha',
            'tree_hash','payload_hash','diff_sha256','approved_at']
        AND publication_approval-ARRAY['approval_id','actor','repository','base_sha',
            'tree_hash','payload_hash','diff_sha256','approved_at']='{}'),
    approved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL CHECK (expires_at>approved_at)
);

CREATE TABLE app.sa_source_review_links (
    link_id uuid PRIMARY KEY,
    review_id uuid NOT NULL REFERENCES app.sa_source_reviews(review_id),
    kind text NOT NULL CHECK (kind IN ('draft_pr','ci')),
    receipt_sha256 char(64) NOT NULL CHECK (receipt_sha256 ~ '^[a-f0-9]{64}$'),
    facts jsonb NOT NULL CHECK (jsonb_typeof(facts)='object' AND octet_length(facts::text)<=8192
        AND facts-ARRAY['mode','source_kind','repository','repository_id','number','head_sha',
            'base_sha','base_branch','branch','draft','ticket_id','run_id','source_input',
            'resource_proof','workflow_id','run_attempt','conclusion','receipt_sha256']='{}'),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(review_id,kind,receipt_sha256)
);

CREATE TABLE app.sa_source_review_events (
    event_id bigserial PRIMARY KEY,
    review_id uuid NOT NULL,
    project_id text COLLATE "C" NOT NULL,
    kind text NOT NULL CHECK (kind IN ('prepared','presented','approved','cancelled',
        'invalidated','body_erased','published','reconciled','native_linked')),
    reason_code text NOT NULL CHECK (reason_code ~ '^SOURCE_REVIEW_[A-Z0-9_]{1,80}$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(review_id,project_id) REFERENCES app.sa_source_reviews(review_id,project_id)
);
CREATE INDEX sa_source_review_history ON app.sa_source_review_events(review_id,event_id);
CREATE INDEX sa_source_review_run ON app.sa_source_reviews(run_id,created_at);

CREATE FUNCTION app.sa_source_review_current(identifier uuid) RETURNS boolean
LANGUAGE sql VOLATILE AS $$
    SELECT EXISTS (
        SELECT 1 FROM app.sa_source_reviews review
        JOIN app.sa_coding_bindings b ON b.coding_id=review.coding_id
        JOIN app.sa_coding_attempts a ON a.attempt_id=review.attempt_id
        JOIN app.sa_coding_verifications v ON v.verification_id=review.verification_id
        JOIN app.runs r ON r.run_id=review.run_id
        JOIN app.tickets t ON t.ticket_id=r.ticket_id
        JOIN app.threads thread ON thread.thread_id=t.thread_id
        JOIN app.actions action ON action.action_id=review.action_id
        WHERE review.review_id=identifier AND review.invalidated_at IS NULL
        AND review.expires_at>clock_timestamp() AND b.state='admitted'
        AND b.valid_until>clock_timestamp() AND a.coding_id=b.coding_id
        AND a.run_id=b.run_id AND a.execution_epoch=r.execution_epoch
        AND a.state='result_recorded' AND a.fenced_at IS NULL
        AND v.attempt_id=a.attempt_id AND v.coding_id=b.coding_id
        AND v.result='passed' AND v.tests_run>0 AND v.tests_failed=0
        AND v.execution_operation_id IS NOT NULL
        AND v.mandate_sha256=b.mandate_sha256
        AND v.environment_sha256=a.environment_sha256
        AND v.profile_sha256=b.profile_sha256 AND v.acceptance_sha256=b.acceptance_sha256
        AND NOT EXISTS (SELECT 1 FROM app.sa_coding_verifications later
            WHERE later.coding_id=b.coding_id AND later.created_at>v.created_at)
        AND NOT EXISTS (SELECT 1 FROM app.sa_coding_attempts later
            WHERE later.coding_id=b.coding_id AND later.created_at>a.created_at)
        AND t.status='open' AND t.mandate_id=b.mandate_id
        AND r.status IN ('running','waiting_approval')
        AND r.graph_version='source-coding-v1' AND r.execution_mode='fixture'
        AND r.execution_epoch=(review.context->>'epoch')::bigint
        AND thread.revision=(review.context->>'input_revision')::bigint
        AND action.run_id=r.run_id AND action.action_type='draft_pr'
        AND action.execution_epoch=r.execution_epoch
        AND action.base_revision=thread.revision AND action.payload_hash=review.action_sha256
        AND action.payload->>'scope_kind'='source-review-v1'
        AND action.payload->>'review_id'=review.review_id::text
        AND action.expires_at=review.expires_at
        AND NOT EXISTS (SELECT 1 FROM app.sa_async_bindings ab WHERE ab.coding_id=b.coding_id
            AND (ab.state<>'active' OR ab.expires_at<=clock_timestamp()
                OR ab.revision<>(review.context->>'async_revision')::bigint
                OR ab.input_revision<>(review.context->>'async_input_revision')::bigint))
        AND NOT EXISTS (SELECT 1 FROM app.sa_async_jobs job WHERE job.coding_id=b.coding_id
            AND job.state<>'known')
    );
$$;

CREATE FUNCTION app.sa_source_review_header_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'review provenance is permanent'; END IF;
    IF TG_OP='UPDATE' THEN
        IF OLD.invalidated_at IS NOT NULL OR NEW.invalidated_at IS NULL
            OR NEW.reason_code IS NULL
            OR to_jsonb(NEW)-ARRAY['invalidated_at','reason_code'] IS DISTINCT FROM
               to_jsonb(OLD)-ARRAY['invalidated_at','reason_code'] THEN
            RAISE EXCEPTION 'review identity is immutable and invalidation is irreversible';
        END IF;
        RETURN NEW;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM app.actions action
        JOIN app.sa_coding_bindings b ON b.run_id=action.run_id
        JOIN app.sa_coding_attempts a ON a.coding_id=b.coding_id
        JOIN app.sa_coding_verifications v ON v.attempt_id=a.attempt_id
        WHERE action.action_id=NEW.action_id AND action.status='proposed'
        AND action.action_type='draft_pr' AND action.run_id=NEW.run_id
        AND action.payload_hash=NEW.action_sha256 AND action.expires_at=NEW.expires_at
        AND b.coding_id=NEW.coding_id AND b.project_id=NEW.project_id
        AND a.attempt_id=NEW.attempt_id AND a.state='result_recorded'
        AND v.verification_id=NEW.verification_id AND v.result='passed'
        AND v.tests_run>0 AND v.tests_failed=0)
        OR NEW.context->>'review_id' IS DISTINCT FROM NEW.review_id::text
        OR NEW.context->>'coding_id' IS DISTINCT FROM NEW.coding_id::text
        OR NEW.context->>'attempt_id' IS DISTINCT FROM NEW.attempt_id::text
        OR NEW.context->>'prepared_by' IS DISTINCT FROM NEW.prepared_by::text
        OR NEW.context->>'profile_sha256' IS DISTINCT FROM NEW.profile_sha256
        OR NEW.context->'target'->>'run_id' IS DISTINCT FROM NEW.run_id::text
        OR NEW.context->'verification'->>'verification_id' IS DISTINCT FROM NEW.verification_id::text
        OR NEW.publication_metadata->>'bundle_sha256' IS DISTINCT FROM NEW.bundle_sha256 THEN
        RAISE EXCEPTION 'review must bind one original Action and actual verification';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_review_header_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_source_reviews FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_header_guard();

CREATE FUNCTION app.sa_source_review_payload_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'review body tombstone is permanent'; END IF;
    IF TG_OP='UPDATE' THEN
        IF OLD.unavailable_at IS NOT NULL OR NEW.unavailable_at IS NULL OR NEW.ciphertext IS NOT NULL
            OR to_jsonb(NEW)-ARRAY['ciphertext','unavailable_at','reason_code'] IS DISTINCT FROM
               to_jsonb(OLD)-ARRAY['ciphertext','unavailable_at','reason_code'] THEN
            RAISE EXCEPTION 'erased review content cannot return';
        END IF;
    ELSIF NEW.unavailable_at IS NOT NULL OR NOT EXISTS (
        SELECT 1 FROM app.sa_source_reviews r WHERE r.review_id=NEW.review_id
        AND r.bundle_sha256=NEW.bundle_sha256 AND r.expires_at>=NEW.expires_at
        AND NEW.expires_at>clock_timestamp() AND app.sa_source_review_current(r.review_id)) THEN
        RAISE EXCEPTION 'encrypted review content requires current exact authority';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_review_payload_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_source_review_payloads FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_payload_guard();

CREATE FUNCTION app.sa_source_review_presentation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'presentation receipts are immutable'; END IF;
    IF NOT EXISTS (SELECT 1 FROM app.sa_source_reviews r JOIN app.web_sessions s
        ON s.user_id=r.prepared_by WHERE r.review_id=NEW.review_id AND r.prepared_by=NEW.user_id
        AND s.token_hash=NEW.session_sha256 AND s.expires_at>NEW.presented_at
        AND r.context_sha256=NEW.context_sha256 AND r.bundle_sha256=NEW.bundle_sha256
        AND r.action_sha256=NEW.action_sha256 AND r.expires_at>=NEW.expires_at
        AND app.sa_source_review_current(r.review_id)) THEN
        RAISE EXCEPTION 'full bundle presentation must bind current Owner session and exact hashes';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_review_presentation_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_source_review_presentations FOR EACH ROW
    EXECUTE FUNCTION app.sa_source_review_presentation_guard();

CREATE FUNCTION app.sa_source_review_approval_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'Owner exact approvals are immutable'; END IF;
    IF NOT EXISTS (SELECT 1 FROM app.sa_source_reviews r
        JOIN app.sa_source_review_presentations p ON p.review_id=r.review_id
        JOIN app.approvals a ON a.action_id=r.action_id
        JOIN app.web_sessions s ON s.user_id=p.user_id AND s.token_hash=p.session_sha256
        WHERE r.review_id=NEW.review_id AND p.presentation_id=NEW.presentation_id
        AND p.user_id=NEW.user_id AND p.session_sha256=NEW.session_sha256
        AND p.expires_at>NEW.approved_at AND s.expires_at>NEW.approved_at
        AND NEW.expires_at<=r.expires_at AND r.bundle_sha256=NEW.bundle_sha256
        AND r.action_sha256=NEW.action_sha256 AND p.bundle_sha256=NEW.bundle_sha256
        AND p.action_sha256=NEW.action_sha256 AND a.approval_id=NEW.approval_id
        AND a.payload_hash=NEW.action_sha256 AND a.actor=r.context->>'actor'
        AND NEW.publication_approval->>'payload_hash'=NEW.bundle_sha256
        AND NEW.publication_approval->>'actor'=a.actor
        AND app.sa_source_review_current(r.review_id)) THEN
        RAISE EXCEPTION 'original Approval must bind the exact current Owner presentation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_review_approval_guard BEFORE INSERT OR UPDATE OR DELETE
    ON app.sa_source_review_approvals FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_approval_guard();

CREATE FUNCTION app.sa_source_review_fact_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'review links and events are immutable'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER sa_source_review_link_guard BEFORE UPDATE OR DELETE
    ON app.sa_source_review_links FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_fact_guard();
CREATE TRIGGER sa_source_review_event_guard BEFORE UPDATE OR DELETE
    ON app.sa_source_review_events FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_fact_guard();

CREATE FUNCTION app.sa_source_review_invalidate(identifier uuid, reason text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE r app.sa_source_reviews%ROWTYPE; at_time timestamptz;
BEGIN
    SELECT * INTO r FROM app.sa_source_reviews WHERE review_id=identifier FOR UPDATE;
    IF NOT FOUND OR r.invalidated_at IS NOT NULL THEN RETURN; END IF;
    at_time:=clock_timestamp();
    UPDATE app.sa_source_reviews SET invalidated_at=at_time,reason_code=reason
        WHERE review_id=identifier;
    UPDATE app.sa_source_review_payloads SET ciphertext=NULL,unavailable_at=at_time,reason_code=reason
        WHERE review_id=identifier AND unavailable_at IS NULL;
    UPDATE app.actions SET status='cancelled',reason_code=reason
        WHERE action_id=r.action_id AND status IN ('proposed','approved','ready');
    UPDATE app.outbox SET status='cancelled'
        WHERE action_id=r.action_id AND status='ready';
    INSERT INTO app.sa_source_review_events(review_id,project_id,kind,reason_code)
        VALUES(identifier,r.project_id,'invalidated',reason);
    -- dispatching/outcome_unknown rows and publication receipts remain intact.
END $$;

CREATE FUNCTION app.sa_source_review_source_changed() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target uuid; source_id uuid; identity uuid; policy uuid; input_identifier uuid; coding uuid;
BEGIN
    IF TG_TABLE_NAME='source_ingress_inputs' THEN
        IF TG_OP='UPDATE' THEN
            IF NEW.unavailable_at IS NOT DISTINCT FROM OLD.unavailable_at
                AND NEW.metadata IS NOT DISTINCT FROM OLD.metadata
                AND NEW.sender_ref IS NOT DISTINCT FROM OLD.sender_ref THEN RETURN NULL; END IF;
        END IF;
    END IF;
    IF TG_TABLE_NAME IN ('source_ingress_inputs','source_ingress_bodies') THEN
        input_identifier:=OLD.input_id;
    ELSIF TG_TABLE_NAME IN ('source_identities','source_role_bindings') THEN
        identity:=OLD.identity_id;
    ELSIF TG_TABLE_NAME='source_policies' THEN policy:=OLD.policy_id;
    ELSIF TG_TABLE_NAME='source_ingress_controls' THEN source_id:=OLD.source_instance_id;
    ELSIF TG_TABLE_NAME IN ('sa_coding_bindings','sa_async_bindings') THEN coding:=OLD.coding_id;
    END IF;
    FOR target IN SELECT r.review_id FROM app.sa_source_reviews r
        JOIN app.sa_coding_bindings b ON b.coding_id=r.coding_id
        WHERE r.invalidated_at IS NULL AND (b.input_id=input_identifier OR b.identity_id=identity
            OR b.policy_id=policy OR b.source_instance_id=source_id OR b.coding_id=coding)
    LOOP PERFORM app.sa_source_review_invalidate(target,'SOURCE_REVIEW_AUTHORITY_CHANGED'); END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER sa_source_review_input_changed AFTER UPDATE OR DELETE ON app.source_ingress_inputs
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_source_changed();
CREATE TRIGGER sa_source_review_body_removed AFTER DELETE ON app.source_ingress_bodies
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_source_changed();
CREATE TRIGGER sa_source_review_identity_changed AFTER UPDATE ON app.source_identities
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_source_changed();
CREATE TRIGGER sa_source_review_role_changed AFTER UPDATE OR DELETE ON app.source_role_bindings
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_source_changed();
CREATE TRIGGER sa_source_review_policy_changed AFTER UPDATE ON app.source_policies
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_source_changed();
CREATE TRIGGER sa_source_review_receive_changed AFTER UPDATE ON app.source_ingress_controls
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_source_changed();
CREATE TRIGGER sa_source_review_coding_changed AFTER UPDATE ON app.sa_coding_bindings
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_source_changed();
CREATE TRIGGER sa_source_review_async_changed AFTER UPDATE ON app.sa_async_bindings
    FOR EACH ROW EXECUTE FUNCTION app.sa_source_review_source_changed();

-- The Engine additionally checks current Source/roles/policy/typed context,
-- actual files, primary/fencing grant and remote full tree at each IO boundary.
-- No credential, listener, model call, remote write, merge or deployment here.
